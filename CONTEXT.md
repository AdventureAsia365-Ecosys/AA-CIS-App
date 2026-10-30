---
id: ARCH-06-cis
title: "AA-CIS-App — Repo Context"
description: Stack, boundaries, ownership, domain terms and working rules of AA-CIS-App (API incl. the Content studio API, job worker, admin app); read it before designing or changing code in this repo.
type: explanation
status: active
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-cis-api, aa-cis-worker, aa-cis-frontend]
last_reviewed: 2026-10-25
review_every_days: 90
related: [ARCH-01, ARCH-05, ARCH-08, docs/adr/README.md]
---

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

Architecture diagram (AWS icons): `AA-Ecosys/docs/architecture/diagrams/cis.svg`, drawn from `cis.py` in the
same folder; the catalog page `AA-Ecosys/docs/architecture/diagrams/README.md` holds every view and is shown in the CIS
admin at `/admin/docs?path=docs/architecture/diagrams/README.md`.

- **Backend**: Python (FastAPI-style routers under `api/routers/`, services under `services/`),
  running on **ECS** (cluster `aa-cis-dev-cluster`, service `aa-cis-dev-api`) fronted by ALB +
  API Gateway. Postgres (RDS) holds the schemas; some steps use Bedrock for LLM work.
- **Frontend**: one Next.js app here, `frontend/` = the **Admin** console (`app/admin/*`), Vercel `aa-cis-frontend`,
  `https://aa-cis.lumiguides.it.com`, workflow `Vercel Deploy` (job "Vercel deploy"; CI, not one of the required
  checks — see Deploy & CI).
  - The **Tenant portal** (Content studio, `https://portal.lumiguides.it.com`) is NOT in this repo: it split off the
    admin as its own app in AA-885 (S243), moved to `AdventureAsia365-Ecosys/AA-Partner/portal` with its history
    in AA-979 (CRM-P2a), runs on Fargate behind CloudFront since the P2b cutover (27/10/2026), and the frozen copy
    left here was deleted at the P2b clean-up (runbook §4.5; `tests/unit/test_aa885_portal_split.py` keeps it
    gone). Read AA-Partner `CONTEXT.md` ("Portal") for its pages, shell, gate and sign-in. The Content studio API
    (`/v1/*`, `/auth/tenant-session`, `/auth/tenant-memberships`, `/auth/tenant-login` for machines) stays here
    (root ADR 0010 §7, ADR 0010a).
  - Admin pages: `dashboard`, `upload` (A0), `s1-rewrite`, `review` (A2 Review Queue — table
    view with a Dismiss action, AA-626; migrated to the UI kit + react-query with multi-select
    bulk regenerate and non-blocking regenerate, AA-662 PR 2 / AA-719), `master-content` (AA-718:
    server-side filter/sort/paginate + filter-aware stat cards + windowed cost + derived pipeline
    status, via react-query — see A3 note below), `atom-curation` (Social Content tabs
    01-05), `tenant-activity` (tabs 06-08), `platform-stats` (includes gate telemetry per tenant
    and channel, AA-615), `cost-ops` (the **Cost Operations** page, AA-622/623/665; the old `/admin/llm-usage` redirects to it), `tenants`,
    `jobs`, `decisions` (**Jev Decisions**, AA-660), `photos` (**Photos**, AA-708 — Drive photo sync, unmatched queue), `api-docs` (**API Docs**,
    admin-only read-only OpenAPI reader built on the kit: `GET /admin/openapi.json` through the BFF, since the
    AA-577 `/docs`, `/redoc`, `/openapi.json` need the X-Admin-Secret header on ECS), `settings` (pipeline gates, **Brand
    Identity** editor, SEO config, LLM model per stage + read-only Jev questions per stage). `run-health` was removed with N7/N8 (AA-603).
  - Admin navigation (AA-663): **Overview** (Dashboard) · **Content** (Upload, Rewrite S1,
    Review Queue, Master Content) · **Intelligence** (Social Content) · **Tenants** ·
    **Operations** (Cost Operations, Jobs, Jev Decisions, API Docs) · **Settings**. Admin-only items are hidden for other
    roles (same list as `lib/rbac.ts` PAGE_RULES, enforced by `proxy.ts`). The legacy `(internal)` route group
    (`/upload`, `/review`, `/catalog`, `/brand`) is deleted; those URLs and `/admin/brand`
    redirect in `next.config.ts`.
  - **The admin host and the portal (AA-885, AA-979)**: the admin answers `/portal`, `/portal/*` and `/tenant-login`
    with **308** to the same path on `https://portal.lumiguides.it.com` (`frontend/next.config.ts`, `PORTAL_BASE_URL`
    in `frontend/lib/apps.ts`) and serves no portal page or tenant BFF. Staff have **no** access to the tenant portal
    (no staff session there; a support view is future work); the staff login does not link to it.
    - No CORS is needed for the portal: its BFF calls the API server to server. The API sends no CORS grant on a
      deployed task (AA-876, `cors_allow_origins()` in `api/main.py`: empty when `AWS_EXECUTION_ENV` is set, only
      `localhost:3000/3001` off ECS); a browser preflight from any origin gets no `Access-Control-Allow-Origin`.
    - The admin app (`frontend/next.config.ts`) sends six security headers on every response (HSTS, `nosniff`,
      `Referrer-Policy`, `X-Frame-Options: DENY`, a minimal CSP `frame-ancestors 'none'; base-uri 'self';
      form-action 'self'; object-src 'none'`, `Permissions-Policy`), plus `poweredByHeader: false` (AA-876).
    - **Cross-site writes (AA-911):** every admin BFF write route (any method but GET/HEAD/OPTIONS)
      first calls `lib/crossSiteGuard.ts` `refuseCrossSiteWrite()` and answers 403 when `Origin` (or, without it,
      `Sec-Fetch-Site`) is not this origin. `it.com` is a public suffix, so every `*.lumiguides.it.com` host,
      including the fixed Preview hosts that serve unmerged PR code, is the same site and its requests carry the
      Lax session cookies. `tests/unit/test_aa911_bff_cross_site.py` keeps every write route on the guard.
    - AA-979: the public white-label page `/trip/[tourId]` lives on the AA-Partner CRM web on the client host (Dev
      `go-dev.`). The admin host answers `/trip/:path*` with a **permanent 308** there (`next.config.ts`,
      `lib/apps.ts` `TRIP_PAGE_BASE_URL`; a 307 during the P2b rollback window, made permanent and the CIS page
      `app/trip/[tourId]` deleted at the clean-up, runbook §4.5). The CRM page reads `GET /v1/trip/{id}` here, which
      keeps `url_alive` fresh. Published `trip_url`s are built on env `TRIP_PAGE_BASE_URL` (default the admin
      host, which forwards).
  - Shared brand tokens (adventure.asia: Fahkwang/Poppins, gold `#DB9628`, pill buttons, logo)
    live in `frontend/app/_kit/core/brand.ts` (was `app/_brand/tokens.ts` until AA-994 S1a) and are used by admin,
    login pages (AA-605; the AA-Partner portal and CRM web vendor them through the kit core). New pages should reuse them.
  - **UI kit core (AA-994 S1a, root ADR 0016)**: the portable part of the kit lives in `frontend/app/_kit/core/`,
    the **canonical copy** for all five frontends (AA-Partner portal and CRM web, AA-Booking admin vendor it byte for
    byte; AA-TripPlanner-Web takes the `tokens` profile). It holds `tokens.ts`, `brand.ts`, `primitives.tsx`,
    `DataTable.tsx`, `csv.ts`, `savedViews.ts`, `Drawer.tsx`, `Modal.tsx`, `Toast.tsx`, `Providers.tsx`,
    `PageFrame.tsx` (the S252 `AdminMain`, which stays an alias, as do `AdminPaneHeader` / `AdminPaneBody`, for
    consumers that have not migrated; CIS admin code uses the `PageFrame*` names since AA-994 S6b, guarded by
    `lib/adminFrame.test.mjs`; marks itself `data-kit-frame`), `GuidePanel.tsx` + `guide.ts` (the content shape; the page passes its content as a
    prop), `theme.ts` (`createThemeStore({ storageKey })` + its restore script) and `useTheme.ts`
    (`createThemeHooks(store)`), `useHydrated.ts`, `useMediaQuery.ts`, `sidebarStore.ts`, `useSidebar.ts`,
    **AA-994 S4a (upstreamed from AA-Booking):** `Section.tsx` (card region with a header band), `KpiTile.tsx`,
    `FilterBar.tsx` (`FilterBar` / `FilterField` / `useUrlFilters`: filters in the URL query), `DateField.tsx` (en-GB
    day field over the native picker), `RowMenu.tsx`, `Money.tsx`, `DateText.tsx`, `format.ts` (pure, import-free:
    `formatMoney`, `formatDay`, `parseDayInput`, `relativeParts`), the one confirm dialog `ConfirmModal` (absorbed
    Booking's `ConfirmDialog`: Figma layout, optional reason, `message` / `danger` aliases), `PageFrame` `width`
    ("full" default, "wide" 1440, "narrow" 880) + header props, and `KitGallery.tsx` (every S4a component in its states,
    admin Settings → UI kit → Gallery; the gated gallery route of the visual job, S1d, will render it too),
    **AA-994 S6a:** `FormControls.tsx` — the form controls every form uses: `Field` (label above, required mark, hint
    or error below, lock icon; a `<label>` that gives its control `aria-describedby` / `aria-invalid` /
    `aria-required`; `as="div"` = a labelled group) around native `Input`, `Select` (`options` / `placeholder` or
    `<option>` children), `Textarea`, `Checkbox` (`indeterminate`) and `Radio`; 40px high (`size="sm"`: 36px, the
    FilterBar height), focus / invalid / disabled states from one stylesheet; every native prop passes through, so a
    page swaps `<input>` for `<Input>` without touching its state. ConfirmModal's reason box is a Field + Textarea.
    **AA-994 button variants (S260):** `Buttons.tsx` — every kind of button, re-exported by `primitives.tsx` (so
    every barrel that does `export * from "./core/primitives"` has them): `Button` (now with a `dashed` variant and
    native / ARIA passthrough: `aria-*`, `data-*`, `role`, `id`, `testId` → `data-testid`, `className` for Tailwind
    layout, `ref`, `onClick(e)`), `LinkButton` (a text-link action; navigation stays `next/link`), `IconButton`
    (icon only, `label` required = aria-label + default tooltip; outline / plain / overlay, xs–lg, danger tone,
    `pressed`), `SegmentedControl` (a labelled radio group, track or chips, arrow-key roving focus), `ChipButton`
    (one toggle chip: aria-pressed, or aria-checked with role checkbox / radio), `Switch` (role="switch", `label`
    required), `ButtonGroup`
    (role="group", spaced or attached), `ListButton` (popup list rows: `role="option"` + `selected`, menu items,
    danger tone) and `Pressable` (an unstyled clickable surface: photo tile, card, select-like trigger); hover /
    focus-visible / disabled states from one hoisted stylesheet. `Tabs` gains `variant="underline"` (role tablist /
    tab, aria-selected, counts, disabled tabs with a reason, arrow keys); the default track variant's markup is
    unchanged. Render-tested in `lib/kitButtons.test.mjs` (transpiles the core, react-dom/server).
    `check-kit-copy.mjs`, **`eslint-kit.mjs`** (AA-994 S1c, see "Kit lint" below), `MANIFEST.json` and **`tokens.css`** (AA-994 S1b: the one `--aa-*` token set of every
    frontend — light and dark palettes, `--aa-scrim` / `--aa-hero-bg`, shadows, `--aa-font-*` as
    `var(--font-*, "<family>")`, and the type / radius / space / z / motion scales from TripPlanner AA-995;
    `app/globals.css` imports it and keeps only CIS-local rules such as the legacy `--bg-*` / `--text-*` variables and
    the shell CSS). Values are the stricter of the former copies (AA-760, S248, AA-812 D6); light gold buttons carry
    dark `--aa-on-accent` text. No other CSS file may define an `--aa-*` variable (apps use `--bk-*`, `--tp-*`,
    `--crm-*`). Rules: every core file starts with the header line "UI kit core (root
    ADR 0016). Canonical: …"; core imports only `./` files of core and `react`, `react-dom`, `next/*`,
    `lucide-react`, `@tanstack/react-table`, `@tanstack/react-query` (no `@/`, no `../`); after any core edit run
    `node scripts/kit-manifest.mjs` in `frontend/` (rewrites `MANIFEST.json`: sha256 per file + the tokens profile).
    **Frontend Build** runs `node app/_kit/core/check-kit-copy.mjs --self` (manifest current, header lines, import
    closure of core and of the tokens profile), `check-kit-copy.mjs --css .` (no `--aa-*` definition outside
    `core/tokens.css`), `lib/kitCore.test.mjs` and `lib/tokensContrast.test.mjs` (WCAG contrast of every checked text
    pair in both themes, plus white text over the photo scrim; helpers in `lib/contrast.mjs`). A consumer runs the same script without
    `--self` against its copy and `kit.lock.json`; copies are written only by `.github/scripts/kit-sync.sh` (the
    root `tools/kit/kit-sync.sh` runs it) and never edited in place (root hook `kit-core-edit-guard.sh`).
    **Kit sync (AA-994 S3s, ADR 0016 decision 6)**: a merge to `main` that touches `core/**` runs **Kit Sync**
    (`kit-sync.yml` + `.github/scripts/kit_sync_bot.py`): per consumer repo (AA-Partner portal + web in one PR,
    AA-Booking, AA-TripPlanner-Web tokens profile) an `aa-ecosys-ci-bot` token narrowed to that repo regenerates
    branch `bot/kit-sync` from its `main` and opens or updates one PR `chore: sync UI kit core to AA-CIS-App <sha7>`
    (no-op when no copied file but `MANIFEST.json` changes; the agent merges it through the merge queue); the same
    workflow writes a nightly freshness table (report only). A PR touching `core/**` runs **Kit Consumers**
    (`kit-consumers.yml`, non-required): core overlaid on each consumer's `main`, then integrity, `tsc --noEmit` and
    the app's lint. CIS-local, outside `core/`: `api.ts`,
    `guideContent.ts` (the guide checks, re-exporting the core shape), the shell (`AdminShell`, `Sidebar`, `Topbar`,
    `BottomNav`, `CommandPalette`, `SessionKeepAlive`), `adminNav.tsx`, `useNavSections.ts`, `useAdminIdentity.ts`,
    `useReviewPendingCount.ts`, `chart.ts` (`CHART_TOOLTIP`, the recharts tooltip style, moved out of `adminUi.tsx`
    in AA-994 S6b), and `theme.ts` / `useTheme.ts` (the core store and hooks bound to `cis_theme`; the
    root layout's restore script is `themeStore.restoreScript`). Pages import the barrel `app/_kit` (which re-exports
    core), never `core/` directly; only the root layout, the login form and the legacy `adminUi.tsx` import core
    files directly (server layout, light login bundle, and no import cycle through the barrel's `Sidebar`).
  - **UI language (AA-994, S255 decision)**: one language at a time — English by default, Vietnamese only when the
    user picks it in the one switch (`core/LangSwitch.tsx`, EN | VI, in the admin `Topbar`). Kit core
    `i18n.ts` (no React: `defineMessages(en, vi)` typed catalogues where a message is a string or a function of typed
    arguments, `translator`, `localized`, `createLangStore({ cookieName })` + restore script, `langFromCookie` for a
    server component) and `useI18n.ts` (`I18nProvider`, `useT(M)`, `useLang`, `useSetLang`, `useLocalized`); no i18n
    package. CIS binds the store to the cookie `cis_lang` (`app/_kit/i18n.ts`; absent === English, mirrored in
    localStorage for other tabs); the root layout runs `langStore.restoreScript` (`<html lang>` before paint) and
    `AdminShell` renders the one `I18nProvider`. Server render and hydration are English, the stored choice applies
    right after hydration. Each area keeps its dictionary next to its code in `messages.ts` / `<name>.messages.ts`
    (the shell: `app/_kit/shell.messages.ts`); tsc refuses a missing, extra or differently typed Vietnamese key.
    Guard `tests/unit/test_aa994_admin_i18n.py`: no Vietnamese text in frontend `.ts` / `.tsx` outside dictionary
    files (comments ignored); the files still carrying some are in `tests/unit/aa994_vietnamese_allowlist.txt`, which
    only shrinks.
  - **Kit lint (AA-994 S1c, root ADR 0016 decision 7)**: `core/eslint-kit.mjs` is a dependency-free ESLint 9 plugin
    shipped with the core (byte-identical in every app; presets `inline` for CIS / portal / CRM web and `tailwind` for
    Booking / TripPlanner). `frontend/eslint.config.mjs` applies `kitConfig({ preset: "inline" })` at `error` to
    `app/**` except `app/_kit/**` (the kit owns the raw elements): no raw `<main>` (PageFrame / AdminMain), `<table>`
    (DataTable), `<dialog>` / `role="dialog"` (Modal / Drawer), no hex / rgb() / hsl() colour, no `<button>` with
    `style` / `className` (kit Button / LinkButton / IconButton / SegmentedControl / ChipButton / Switch / ListButton / Pressable), no `var(--aa-*)` string (use `K`), no `--aa-*` set from JS, no import of
    `app/admin/_components/adminUi` or the portal `ui.tsx` (`no-legacy-kit`), no `className`, and (AA-994 S6a) no
    raw `<input>` / `<select>` / `<textarea>` (`no-raw-form-control`: the kit Field + controls or DateField;
    `type="hidden"` / `"file"` stay allowed). Rule 8 is **staged**: it is in the plugin but in no preset, and an app
    turns it on with `kitConfig({ …, rules: STAGED_RULES })` in the PR that freezes its existing uses (CIS first;
    Partner and Booking with their page migrations; the AA-994 close-out moves it into the presets), so a kit sync
    never turns a consumer's lint red. Violations that existed on 27/10/2026 (rule 8: on 29/10/2026, 239 in 53 files)
    are frozen in `frontend/eslint-suppressions.json` (ESLint bulk suppressions); the file only shrinks.
    **Frontend Lint** (a gate, no `continue-on-error`; to be made required) runs the rules' RuleTester suite
    (`lib/eslintKit.test.mjs`), `eslint --prune-suppressions`, fails when the committed file differs from the pruned
    one, and fails when an entry or a rule total grows against the base branch (`check-kit-copy.mjs --suppressions`);
    a new entry may only carry the violations of a deleted / renamed file into a file the base did not have, so fixing
    one old violation never buys a new one elsewhere. The one exception is a rule the change switches on:
    `frontend/scripts/kit-new-rules.mjs <base-sha>` rebuilds the base ESLint config from git and lists the rules that
    are on at the head and off at the base for every file that suppresses them; `--new-rules` lets such a rule (with no
    base suppressions) start its baseline once, after which it only shrinks. Inline disables of kit rules are not allowed: CI
    (`check-kit-copy.mjs --no-kit-disable app --ignore _kit`) fails on an `eslint-disable…` that names an `aa-kit/`
    rule, on a bare `eslint-disable` / `-line` / `-next-line` (it switches every rule off) and on a
    `/* eslint aa-kit/…: off */` comment in page code; the suppressions file is the only baseline. Disables that name
    other rules (`react-hooks/…`, `@next/next/no-img-element`) stay allowed.
    After fixing old violations: `npx eslint --prune-suppressions` in `frontend/` and commit the file; never
    `--suppress-all` to hide a new one. The pre-push hook (`scripts/pre-push.sh`, check `frontend_kit` + `frontend_lint`)
    runs the kit self-check, the CSS check, the no-kit-disable check, the suppressions growth check and ESLint on the
    changed frontend files (all of `frontend/` when the config, the rules or the suppressions changed).
  - **UI kit (AA-662, `frontend/app/_kit/`)**: the shared component library for UI v2, built on
    TanStack Table (`@tanstack/react-table`) and brand tokens (inline styles, no Tailwind
    classNames). Exports `DataTable` (per-column filter, multi-sort, column show/hide, pagination,
    sticky header, row selection + bulk actions, saved views in localStorage per user, CSV export),
    `Drawer`, `Modal`/`ConfirmModal`, `PageHeader`, `Tabs`, `Badge`/`StatusBadge` (one `tone`
    vocabulary; `StatusBadge` is driven by job-runner status strings), `EmptyState`, `ErrorState`,
    `Skeleton`, `ToastProvider`/`useToast`, `ScrollX` (S243), `InfoTip` (S243: (i) button + tooltip, hover / focus /
    tap to open, Escape or a tap elsewhere to close, text tied by `aria-describedby`, hoverable bubble, 24 px hit
    area), `GuidePanel` + `GuideContent` (AA-919: the one Guide-tab pattern — every admin page with a Guide tab
    keeps its content as data in `app/admin/<page>/guide.ts`, renders it with `GuidePanel` (numbered sections, an
    "On this page" contents list for long guides, note / warning callouts, label chips, step diagrams, control cards),
    adds the "Guide" tab after its daily-use tabs and a "New here? Read the Guide" link in its description, and is
    registered in `frontend/lib/guides.test.mjs`, which checks the shape and that every documented label is still on
    the page), `AdminMain` (S252: **the one content frame of every admin page** — standard padding 28/36/56 on
    desktop, the `.aa-admin-main` phone rule (16px, 84px above the BottomNav), its own vertical scroll and
    `minWidth: 0` so a wide table scrolls inside its card, never the page; `variant="split"` with `AdminPaneHeader` /
    `AdminPaneBody` for pages with a sticky header band (same 36px / 16px insets, no double inset on phones); every
    `app/admin/**/page.tsx` renders it once together with the kit `PageHeader`, and no admin file hand-writes a
    `<main>` or the frame classes — guarded by `frontend/lib/adminFrame.test.mjs`), and `apiGet`/`apiSend` fetch
    helpers. New pages import
    from `app/_kit`. The legacy primitives (`admin/_components/adminUi.tsx`,
    `auditPanels.tsx`, `FilterBar.tsx`, `Pagination.tsx`, portal `_components/ui.tsx`,
    `Skeleton.tsx`) still stand — pages are migrated to the kit one at a time (Review Queue and My
    Content first). Living reference: the **UI Kit tab in admin Settings** (`/admin/settings?tab=ui-kit`,
    `app/admin/_components/SettingsKitTab.tsx`) — renders every primitive with static data.
  - **Data fetching (AA-662)**: `@tanstack/react-query` is the standard for UI v2, wired app-wide
    via a single `QueryClientProvider` in `app/_kit/core/Providers.tsx`, mounted from the server root
    layout (`app/layout.tsx`) around all routes. Defaults: no refetch-on-focus, one retry, 30s
    stale. Pages that poll opt in per-query with `refetchInterval` (replaces the old hand-rolled
    `useState`/`useEffect`/`setInterval` fetch). Older pages keep their hand-rolled fetch until
    migrated.
- **AI/Bedrock**: LLM calls (rewrite, research, gates) route through the ecosystem Bedrock path
  on the acc3 satellite only (AA-998, S255: acc1, the former cross-account fallback, left the ecosystem;
  an acc3 failure falls to a same-account model or the job retry); the cross-account trust is provisioned in
  AA-CIS-Infra.
  Embeddings (Cohere Embed v4) run natively on acc2, through the GLOBAL cross-region profile
  `global.cohere.embed-v4:0` since migration 238 (AA-856: the `us.` quota is fixed at 20 rpm, the global
  one is adjustable; one quota shared by f10_embed, a3_place_embed and TripPlanner search). Claude cannot be invoked from a local shell — the satellite roles only trust the
  ECS task role, so real LLM measurements run inside the `api` container via ECS exec.
- **Model per stage is DB-driven**: `shared.llm_role_config` (20s cache; `SAFE_DEFAULTS` in
  `shared/llm_client/role_config.py` are only a fallback), editable in admin Settings.
- **Model Catalog** (`shared.llm_model_catalog`, AA-658, ADR 0005): the admin dropdown options and
  LLM prices come from it (`pricing.py` is the fallback). Non-legacy Model Keys (e.g. `sonnet-5`)
  are called with the Bedrock Converse API on the acc3 satellite.
- **Connection registry + Anthropic API path** (AA-1002, gateway slices S1 / S2; migrations 304 / 306):
  `shared.llm_provider_connection` (one row per way in: Bedrock account, vendor API; a secret NAME only,
  `^aa-llm/<env>/<name>$`, and a code allowlist of base URL hosts per kind) and `shared.llm_model_deployment` (a
  catalog model on a connection: wire id, priority, `request_defaults`). Only `shared/llm_client/vendor_route.py`
  reads them on the call path: a catalog key whose first enabled deployment (lowest priority) on an enabled
  connection is on an `anthropic` connection is called through the Anthropic Messages API
  (`adapters/anthropic_messages.py`, SDK `anthropic` 2.x: key read from the secret at call time, per-connection
  timeout / retries, `output_config.format` for structured output, explicit `output_config.effort`, no sampling
  parameters, no forced tool, no server-side fallbacks); every other registry state keeps today's path byte for
  byte. A `model_tier` of `<model key>@<connection>` pins one deployment (probes only). The response is labelled
  `anthropic-api-<key>` so `llm_call_log` records `provider = 'anthropic'`; `tokens_in` = uncached + cache read +
  cache write (the Converse convention); a refusal / empty / non-conforming reply is billed-unusable; 401 / 403 are
  `access_denied`, 402 / credit or spend-limit wording (400 / 402 / 403 only) `quota_exhausted`, 429 / 529
  `throttled` (checked first). The key comes from `secret_name` only (a credential env is tied to its own kind).
  Migration 306 seeds Anthropic API deployments behind acc3 (priority 100, `vendor_direct`, every Anthropic Converse
  model, tenant ones included) with the `anthropic-direct` connection off: slice S5 data scopes must land before or
  with S3's spread over deployments, or tenant content leaves AWS when acc3 fails; the only admin write in S2 is
  `PATCH /admin/llm-connections/{key}` `{"enabled": bool}` (Anthropic connections, audited). Breaker, data scopes
  and cost parity are later slices (S4–S6).
- **Deployment routing + legacy aliases** (AA-1002 slice S3, migration 307): a catalog key is tried on its
  deployments (`vendor_route.deployment_plan`): the model's callable rows (Bedrock assume_role / task_role for
  converse, anthropic_native and embed rows; Anthropic for Anthropic non-legacy rows; OpenAI only for
  provider='openai' rows), enabled and on an enabled connection, lowest priority first, the same model on each
  deployment before the route's next model. Until slice S5: a `vendor_direct` connection is used only as the FIRST
  enabled row (an admin's deliberate choice), never as a fallback behind an `aws_only` one, decided before the circuit
  check (`vendor_route.circuit_open` = `breaker.is_open`, slice S4). No callable row (or no registry) =
  today's path; callable rows that are all off = the model is refused and the route moves on. A billed unusable reply
  is the model's answer (not retried on another deployment). With the 304 / 306 rows every Dev stage makes exactly
  the calls it made before S3 (`tests/unit/test_aa1002_s3_routes_snapshot.py`, golden from pre-S3 code).
  `LLM_DEPLOYMENT_ROUTING=off` (ECS env) restores the S2 path. Legacy keys: `shared.llm_model_alias` maps `haiku` →
  [haiku], `sonnet` → [sonnet, haiku] (the chain's order); with `LLM_LEGACY_ALIASES=on` (default OFF until a live check)
  each target runs on its deployments with the legacy InvokeModel body (the very requests of `_legacy_chain`); no alias
  row / no registry / `LLM_LEGACY_ACC2_NATIVE=on` = `_legacy_chain`. Accounts are data: `bedrock_satellite` builds
  STS sessions from the assume-role connection (cache key: connection, role ARN, ExternalId, region),
  `catalog.serving_accounts()` and `embed._runtime_for` read the registry; `connections.BUILTIN_CONNECTIONS` (the 304
  seed: acc3, acc2, openai-direct) answers only "how to reach X" when the registry is unreadable or a row is broken.
  Still contract work (not in 307): the logical rows `sonnet-4-6` / `haiku-4-5` and the `account_route` CHECK → FK.
- **Circuit breaker + limiter + Connections tab** (AA-1002 slice S4, migrations 309 expand / 310 contract):
  `shared/llm_client/breaker.py` keeps one circuit per deployment (connection × catalog model) in
  `shared.llm_provider_health`, shared by every api / worker task (10 s snapshot; writes lock the row and apply pure
  transitions). Class rules (`breaker.RULES`): access_denied / quota_exhausted / `credential` (a key that cannot be
  read) open at once, throttled after 3 in a row, anything else after 5; a refusal of one request's own content
  (input too long, malformed, content filter: breaker-only class `request`) is never counted; open periods are short (30 s – 5 min first,
  doubled on each failed half-open probe, capped). Half-open = exactly one call across tasks takes a probe lease.
  `deployment_plan` leaves out open deployments (`DeploymentPlan.circuit_skips`); a model none of whose deployments
  may be called raises `breaker.CircuitOpen` carrying the class that opened it (the legacy alias path too), so an
  account-level refusal still ends in `LLMAccessDenied` without a provider call. `LLMClient._call_target` and `embed` wrap every registry call in
  `breaker.guarded` (probe admission, the connection's limiter, outcome; an unusable reply counts as an answer). The
  limiter is per process: `max_concurrency` (semaphore) and `rpm_limit` (rolling minute) from the connection row; no
  slot within 30 s = `LimiterSaturated` (throttled, not counted). Table missing / DB down = the same breaker in
  process. `LLM_CIRCUIT_BREAKER=off` (ECS env) = the S3 behaviour. Admin (ai.admin, audited):
  `PATCH /admin/llm-connections/{key}` switches bedrock / anthropic / openai connections and edits the secret NAME
  (`aa-llm/` only), base URL (kind allowlist), timeout, retries, limits, budget, notes (310 grant; identity and data
  scope stay migration-only); `POST /admin/llm-connections/{key}/test` sends one small real call through
  `LLMClient.probe` (works on a switched-off connection; stage `adhoc_conn_test`, role `validate`, logged + audited;
  its outcome counts on the circuit). `GET /admin/llm-availability` adds `connections` (status per connection and the
  circuit of each deployment). UI: Settings › LLM Models › Connections (`frontend/app/admin/settings/ConnectionsTab.tsx`).
- **Data scopes per stage** (AA-1002 slice S5, migration 311 contract, app ADR 0012): `shared.llm_role_config.
  allowed_scopes` (`{aws_only}` or `{aws_only, vendor_direct}`, default `{aws_only}`, migration-only: `cis_app` has no
  INSERT / UPDATE on the column) says which connections (`data_scope`) a stage's content may use. Backfilled from the
  routes (both for the stages whose route ends on `gpt-6-luna-openai`, the S235 last resort), so no call changed
  (`test_aa1002_s3_routes_snapshot.py` `*_scoped` cases). `shared/llm_client/data_scope.py`; the scopes are bound per
  call in `LLMClient._generate` (`client.stage_scopes`) and filter `vendor_route.deployment_plan` /
  `vendor_deployment_for`, the OpenAI adapter (`openai_scope_allowed`, second gate after the S235 catalog gate) and
  `embed`. NULL / no row / column missing = no stage policy = the pre-S5 rules (S3 first-row rule, S235 gate). Admin:
  `GET /admin/llm-config` returns `allowed_scopes` read-only (Route editor › Data), options outside the scope are
  unavailable with a reason, and the PATCHes refuse such a model (422) and keep the S235 position rule in scope terms
  (a model reached only outside AWS: last fallback only). A new stage seeded with an OpenAI-API last resort must set
  both scopes in its seed migration.
- **Cost parity** (AA-1002 slice S6, migration 312 contract): `pricing.quote(model, Usage, deployment)` prices one
  billed request from its full usage (uncached input, output, cache reads, 5-minute writes 1.25x, 1-hour writes 2x,
  Message Batches 50% off every token, Anthropic `inference_geo: "us"` 1.1x, Claude Haiku 5.5 x5 above 100K prompt
  tokens) on the deployment it ran on (`llm_model_deployment.price_*_override` first, then the catalog, then
  `COST_TABLE`, then a flagged estimate at Sonnet rates: never 0) and returns `Quote(cost_usd, pricing_mode)`.
  `calc_cost` is a wrapper with the pre-S6 numbers (golden `tests/unit/snapshots/aa1002_s6_pre_s6_costs.json`, the
  Haiku 5.5 tier the only change). The Converse, Anthropic API and OpenAI paths price through `LLMClient._quote`
  and set `LLMResponse.pricing_mode`; OpenAI cached prompt tokens (`prompt_tokens_details.cached_tokens`) are billed
  at the cache-read rate and reported as `cache_read_tokens`. `llm_call_log.cache_read_tokens / cache_write_tokens /
  pricing_mode` (`override|catalog|code_table|estimate[+batch][+geo_us]`) are written only for a row that has
  something there (cache tokens, a stated mode, or an unknown model: `estimate` even when the caller is silent);
  other rows keep the pre-S6 INSERT, an AA-729 relabelled INSERT is never replaced, and before 312 the write falls
  back to the old statement. `llm_call_daily` gains the cache sums and `estimate_calls` (rollup switches to the S6
  statement once both tables have the columns). Every call site that logs `model=resp.model_used` also passes
  `**call_log.billed_fields(resp)` (cache split, pricing_mode, vendor `connection_key`; AST guard
  `tests/unit/test_aa1002_s6_billed_fields.py`); the judge paths carry the same keys (`brand_fit.RUN_BILLING_KEYS`,
  T10 `invoke_judge`). A vendor-API row stores its connection key as `account` (`openai-direct`,
  `anthropic-direct`; `LLMResponse.connection_key`, never `satellite_account`, which the pipeline copies to
  `generated_content` under an AWS-account CHECK); the admin names it by vendor ("OpenAI API",
  `accounts.VENDOR_CONNECTION_LABELS` = `frontend/lib/awsAccounts.ts`). **Prompt caching on Converse**: a Claude
  profile (`anthropic.` in the wire id) whose system prompt reaches the model's minimum cacheable length (512 tokens
  on the 5.x models, 1024 Sonnet 4.6 / 5, 4096 Haiku 4.5; chars / 4) ends its system list with
  `{"cachePoint": {"type": "default"}}`, so tools + system are cached and the user message is not; Luna / OpenAI on
  Bedrock never get one; a ValidationException naming the cache point is retried once without it and remembered;
  ECS env `LLM_CONVERSE_CACHE_POINT=off` restores the pre-S6 body.
- **Structured output (`LLMRequest.json_schema`) per path**: Converse = one tool whose input is the schema, reply
  conformed (`_checked_json`, AA-659/714); OpenAI = strict `response_format`. The legacy `haiku` / `sonnet` keys
  (Anthropic Messages API, `invoke_claude`) have neither: since S251 (AA-835) the gateway appends a "one JSON object
  matching this schema" instruction with the schema to the system prompt, takes the reply's JSON object (a ``` fence
  is tolerated, prose is not), conforms it, and raises `BilledUnusableReply` otherwise (gateway billed_unusable row,
  the route moves on). It warns `llm_legacy_key_structured_output` once per stage. A structured stage belongs on a
  catalog key; `tests/unit/test_s251_legacy_structured_output.py` lists the structured stages and the one known
  legacy primary (`a3_place_dedupe_check`). A structured request never streams live text (any path). A bad reply is
  typed: `UnusableReply` (`BilledUnusableReply` from one key, `RouteUnusableReply` when every model of a route
  answered unusably; an outage stays a plain `RuntimeError`); callers that must not retry or stop on a bad reply catch
  it, charge `unusable_reply_cost(e)` to their run budget and do not log again (supply writer → `DraftUnusable`;
  place dedupe check → `unsure` for that pair).
- **Stage Route** (AA-659, ADR 0006): each stage has a model plus an ordered fallback list and an
  optional shadow model (A/B, `shared.llm_shadow_log`). Only models the route lists are ever
  tried. No judge has its own hardcoded model path any more. **S235 rule (Nghiệp, 14/10/2026,
  migration 224): Bedrock first; an OpenAI platform API model may only be the LAST fallback of a
  route (a Bedrock outage), never a primary or a shadow; GPT-4.1 is retired.** Judge routes on Dev:
  - `s1_judge`, `t2_judge`, `t7_debate_judge`, `t10_judge`: GPT-5.6 Luna (Bedrock acc3) → GPT-6 Luna
    (Bedrock acc3) → GPT-6 Luna (OpenAI API, last resort); shadow GPT-6 Luna at the admin-set rate.
  - **One judge client** (AA-765): every judge request — S1/T2 brand fit and the T7 Debate candidate fit
    (`brand_fit._judge`), the T10 F9 judge (`quality_gates` via `invoke_judge`) — is built and sent by
    `shared/llm_client/judge.py` (`call_judge`; it also holds the judge JSON parsers). It writes no
    `llm_call_log` row: the callers (judge_node, debate, quality_gates) do, once they have the verdict.
  - `s1_brand_audit`, `t2_brand_audit`: GPT-5.6 Luna → GPT-6 Luna (no OpenAI last resort).
  - acc3 is the only satellite account (AA-998). The legacy Haiku/Sonnet chain is acc3 Sonnet → acc3 Haiku
    (no cross-account tier); `account_route` is CHECKed to acc3 / NULL (migration 295) and the gateway ignores any
    other value. `CatalogModel.account_for` only picks acc3 or acc2. They no longer try acc2-native Bedrock first (AA-843:
    acc2 is a channel-program account and always refuses Anthropic models); the env var
    `LLM_LEGACY_ACC2_NATIVE=on` restores that first attempt if acc2 ever gains model access.
  - A call served by the OpenAI API logs `llm_openai_fallback_used` and is recorded in `llm_call_log`
    with `provider = 'openai'`, `fallback_used = true`.
  - **Brand audit fails closed** (AA-714): a model/schema error gives `manual_check` +
    `BRAND_AUDIT_UNAVAILABLE` (held for review), never `pass`.
  - **Where a GPT model runs**: on Bedrock only on acc3 (acc2 has no OpenAI models), or on the
    OpenAI platform API (`gpt-6-luna-openai`, last resort only). `gpt-4.1` is disabled (migration 224)
    and admin cannot re-enable it; `LLMClient._call_openai` refuses any model whose catalog row is not
    enabled, so the legacy Haiku/Sonnet chain no longer falls back to GPT-4.1. Admin rejects an
    OpenAI-API model as primary, shadow or non-last fallback. Settings › LLM Models shows each route.
- **Account-level refusals fail fast** (AA-900, `shared/llm_client/errors.py`): a provider error is classified as
  `access_denied` (Bedrock "Error 002: Access to Bedrock models is not allowed for this account", AccessDenied, IAM,
  OpenAI 401/403), `quota_exhausted` (no OpenAI credit), `throttled` or `other`. When EVERY model a request tried was
  refused at account level (route, single key or the legacy Haiku/Sonnet chain), the gateway raises `LLMAccessDenied`
  (a RuntimeError; a mixed or throttled failure stays the plain RuntimeError). The job runner
  (`worker.failure_outcome`) fails such a job at once, no retry, error prefixed `llm_access_denied`; the S1/T2 writer
  re-raises it out of the graph (`graph.generate_node`, `s1_writer_v2_live._call`, `_rewrite_tour`) and
  `_run_tour_safe` stops its internal retries. Every other `_rewrite_tour` caller (eval regression, writing-memory
  eval writers, the T3 QA gate) also gets the exception instead of a `failed` dict, so an eval run aborts rather than
  record one refused tour after another. A refusal after a paid draft keeps the draft: the S1 sonnet upgrade
  (`_sonnet_upgrade` returns None) and the writer-v2 meta calls. Per-item loops that swallow LLM errors (A3 atomize
  days) are unchanged.
  Settings › LLM Models › Stages opens with the **LLM availability** card (`GET /admin/llm-availability`,
  `api/services/llm_availability.py`): answered calls per provider / account from `llm_call_log` plus classified
  errors of failed (dated by `finished_at`) or re-queued jobs from `shared.job`; no probe call. A refused call writes no `llm_call_log` row (AA-927 bills
  only), so job errors are the refusal side of the signal.
- **Every model call goes through the gateway** (`shared/llm_client/`, AA-685): text through
  `LLMClient.generate(stage=...)`, embeddings through `shared.llm_client.embed.embed(stage, texts)`
  (catalog `api_style='embed'`, stage role `embed`). `tests/unit/test_aa685_no_raw_model_calls.py` fails
  on any raw Bedrock/OpenAI call outside `shared/llm_client/` (no exceptions since AA-757).
  **One `llm_call_log` row per billed call** (AA-927):
  - one row per `generate()` / `embed()` response returned to a caller, written by the caller, also when
    the reply then turns out unusable. Canonical pattern: write the row right after `generate()` returns
    (T9 `_generate_full`); where the row needs post-processing, keep a `logged` flag and call
    `call_log.record_unusable_sync(resp, ...)` in the except branch;
  - one row per billed provider attempt the gateway throws away (Converse schema retry failing again,
    empty content, invalid JSON / schema on OpenAI) — written by the gateway itself
    (`quality_signal.gateway_attempt = true`), fire-and-forget, before the route moves on or raises;
  - a gateway-internal retry that succeeds (the AA-714 Converse schema retry) is ONE response whose cost
    and tokens cover both requests (`LLMResponse.provider_requests = 2`);
  - a judge verdict built from several calls (AA-714 near-band repeat, AA-801 median3) writes one row per
    call in one transaction (`record_calls_sync`): the verdict row (run 1's cost, `judge_calls: n`) plus
    `{judge_run, of, verdict_row: false}` rows;
  - rows for unusable / discarded / extra calls carry `billed_unusable: true` (or `verdict_row: false`)
    and none of the ok-rate keys admin LLM ops reads (`call_log.OK_RATE_KEYS`), so ok-rates do not move.
  Exclusions: a Jev verdict-cache hit (no provider call; counted in `decision_cache_hits_daily`), a
  stream that broke before its usage event (cost unknown), and S1 Bedrock Batch (no row; batch has never
  run, AA-624 — it must log once enabled). `tests/unit/test_aa927_llm_call_site_registry.py` fails on a
  new LLM call site that is not registered with the function writing its row. Stages added: `a0_column_map`, `f10_embed`, and `tp_compose` /
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
    `typesafe`, role `validate`), so Jev shows in Cost Operations (Jev tab). Admin page: `/admin/decisions`.
    Key: secret `aa-cis/dev/typesafe`. Migration 181.
- **Cost observability (epic AA-616)**:
  - `shared.llm_call_log` records account (acc3 / acc2; rows before AA-998 may hold acc1), provider and
    `fallback_used` per call
    (AA-617), with correct Haiku 4.5 pricing and cache tokens (AA-635).
  - `shared.dfs_call_log` records each DataForSEO call, its real `cost` and cache hits (AA-618).
    The DFS cache TTL is 7 days (AA-625).
  - `shared.dfs_balance_snapshot` + a daily Lambda alert on low DFS balance (AA-627).
  - `shared.cost_explorer_snapshot` holds real AWS Cost Explorer figures for acc2/acc3,
    fetched on demand through `POST /admin/cost-explorer/check` (AA-623). acc2 calls CE
    directly; acc3 via assume-role (`shared/aws_client/cost_explorer.py`). AA-998: acc1 is no longer fetched; its
    stored rows stay and read with `account_key = "retired"` (shown as "Retired account", no account ID).
  - **How the admin names an AWS account** (AA-998): never by the internal key (acc2 / acc3 stay as keys in
    `account_route`, `llm_call_log.account` and the cost rows) but by its 12-digit account ID, read at run time
    (acc2: STS of the task's own credentials, once per process; acc3: `BEDROCK_ACCT_PRIMARY`) by
    `shared/aws_client/accounts.py` and served by `GET /admin/aws-accounts`. The frontend names accounts only
    through `frontend/lib/awsAccounts.ts` (`accountName` / `accountLabel`, hook `useAwsAccounts`); a guard test
    fails on a key spelled anywhere else in the frontend. Labels built by the API (Settings › Models "via",
    the account-route option, the forecast and Cost per app lines) use the same module.
  - The admin **Cost Operations** page (`/admin/cost-ops`, AA-665; was External Spend at `/admin/llm-usage`, which
    redirects) has the tabs Overview, LLM, DataForSEO, AWS bill, Jev and Budgets. It compares estimated spend (token
    logs) with the real AWS bill (AWS bill tab), splitting Bedrock from infrastructure per account, over any date range.
    - Overview: the **month-end forecast vs budget** (`GET /admin/cost-ops/forecast`, `api/services/cost_ops.py`):
      per provider family (Bedrock with the acc3/acc2 split, OpenAI, Jev, DataForSEO) and per AWS account (acc2,
      acc3; a `retired` row only for a month that still holds acc1 history),
      month to date + mean of the last 7 complete days x days left. DB only: the AI rows read
      `llm_call_daily`/`llm_call_log` like Cost per app, the AWS rows the STORED Cost Explorer snapshots (never a live
      CE call). Budgets: `spend_budget` global daily cap x days in the month; AWS from `cost_ops.AWS_MONTHLY_BUDGET_USD`
      (env `COST_OPS_AWS_BUDGETS_USD` overrides) until the AWS Budgets are read live (AA-665 C5).
    - DataForSEO › Runs: `shared.dfs_call_log.job_id` (migration 293) is the job running when the call was made
      (`call_log.current_job_id()`, as for `llm_call_log`); `GET /admin/dfs-usage/runs` groups spend by job with its
      kind, starter, payload scope, status, error and the job's LLM cost; `infer_unlinked=true` matches rows without a
      job (older rows) to the jobs running at the time, best effort, or to a recorded cost incident when no job ran.
    - Recorded cost incidents (`api/services/cost_incidents.py`): the reviewed findings of a spend investigation
      (trigger, scope, failure point, cause, what changed since) kept as code records; the DataForSEO tab shows each
      one overlapping the window, with its numbers read live from `dfs_call_log` / `llm_call_log` (rollup
      `llm_call_daily` after the raw retention). First record: the 25/09/2026 research sweep.
    - Jev: the Jev Decisions endpoints (`/admin/decisions/summary`, `/admin/decisions/jev-credit`).
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
  (`shared/ops_alerts.py`, `shared/aws_client/ses_mailer.py`; env `SES_FROM_ADDRESS`,
  `SES_CONFIGURATION_SET` from AA-CIS-Infra). At most one e-mail per budget,
  threshold and UTC day (job idempotency key); `SES_FROM_ADDRESS` unset = nothing queued, one log line.
  Recipients (AA-913) are resolved when the job runs: the **confirmed** rows of `shared.alert_recipient`
  (Platform › Alert recipients, below) plus `ALERT_EMAIL_TO` (Terraform fallback, always included), deduplicated,
  all in one message; pending rows get nothing. When `ALERTS_TOPIC_ARN` is set the job first reconciles the table
  with SNS (best effort). Nobody to mail = `skipped`. Template `test` = the page's "Send test e-mail".
  **Jev credit (AA-845):** the same job sends `jev_credit_exhausted` (queued when the AA-720 402 bell
  `platform.jev_credit.exhausted` is written) and `jev_credit_low` (`shared/jev_credit.py`: estimated TypeSafe
  credit = Settings › Jev Credit top-ups − logged typesafe spend, below $3 → bell `platform.jev_credit.low`; checked
  every 30 min by the api ops sampler and on `POST /admin/decisions/jev-credit/check`). Throttle: one exhausted
  bell per 24 h while unread (AA-720), one low bell per 24 h read or not; e-mail only with a new bell, at most one
  per template per UTC day. The daily 402 sweep alerts only on real 402s after the latest recorded top-up and the
  latest successful canary. An explicit `threshold_usd` / `test_exhausted_alert` on the check route sends marked
  test alerts (admin bell only) that never throttle a real one and never show as the card's "last alert".
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
    `s1_seo_prefetch`, `photo_sync`, `photo_medium_backfill` — AA-995, `photo_destination_match` — AA-671, `s1_rewrite`, `s1_batch_ingest`, `revalidate`, `recompute`,
    `place_cards_build`, `t7_slate`, `t8_angles`, and the place pipeline `place_extract`,
    `place_geocode`, `tour_graph_build` — AA-712, `place_shape`, `place_dedupe` — AA-847, `place_relocate` — AA-997, the five `retention_*` kinds — AA-825,
    `ops_alert_email` — AA-828, `data_erasure` — AA-839, the writing-memory kinds `writing_signal_backfill`,
    `writing_signal_fact_filter`, `writing_memory_mine` — AA-917, `writing_memory_r0` — AA-918, `writing_memory_r2`,
    `writing_memory_shadow` — AA-919, `writing_memory_post_activation` — AA-920, `writing_memory_kpi_weekly` — AA-922, `kpi_snapshot_daily`, `report_generate`,
    `inquiry_handoff_check`, `report_email` — AA-1003). Every claim merges `progress.attempt_started_at` (AA-751, F18):
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
    `s3://<bronze bucket>/archive/<table>/yyyy/mm/` (`shared/aws_client/bronze.py`) as gzipped JSONL (pyarrow is not a
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
  - **Data erasure (AA-839, DM-6.7, runbook AA-Ecosys `docs/legal/dsar-erasure-runbook.md`)**: one background
    kind `data_erasure` (`services/jobs/erasure_job.py`, concurrency 1, no retry) for a **tenant** (`tenant` =
    tenant id) or a **traveller** (`email_sha256` = sha256 hex of the trimmed, lowercased address; the API
    refuses anything else, so an address never reaches a payload or a log). `POST /admin/job-runner/erasure/run`
    (admin, `tenant.write`) enqueues a **DRY RUN** by default: SELECT only, rows per CIS table and action,
    blockers, the delete order, the tenant's S3 objects/secrets (best-effort) and the per-app checklist
    (`booking.*`, `tripplanner.*`, `crm.*`, Cognito, mailbox, Sentry), plus a `fingerprint` of the subject and
    the counts (v2: the tenant id, or the address hash with a per-DRY-RUN salt that is stripped with the hash;
    a v1 DRY RUN, counts only, cannot be applied).
    Inventory: `services/erasure/inventory.py` — every CIS table with a tenant key has an action (`delete`,
    `detach` = keep the cost row without the tenant link, `merge` = add into the NULL-tenant rollup row, `keep` =
    the append-only `acp_shared.audit_log`). A tenant key is `tenant_id`, `tenant` (`shared.pipeline_jobs`),
    `owner_scope` (`acp_contract.tour_atoms`: tenant-owned T5 atoms may sit on an AA master tour) or a "tenant"
    soft key without an FK (`acp_contract.atomize_day_fingerprint.tenant_tour_version_id`); such entries carry
    explicit predicates (`where_any`) and are deleted before the tables those predicates read. A tenant-key table
    without an entry is reported `unregistered` and fails `tests/integration/test_aa839_erasure_job.py`. Rows
    that reach a tenant only through foreign keys are found from `pg_constraint` at run time
    (`services/erasure/plan.py`) and deleted children-first; a row of another tenant, a kept row, another
    owner's row, or a row of the same table outside the erasure (self FK: `shared.job.parent_job_id`,
    `tenant_tour_versions.parent_version_id`) pointing at an erased row is a blocker. Each check runs in a
    savepoint, so an unreadable table is one blocker, not an aborted DRY RUN. S3 keys held by deleted rows
    (`content_exports`, `webhook_deliveries`, `raw_sources`, `tenants`) outside `raw-inbox/<t>/` and
    `brand-identity/<t>/` are listed in `s3_refs` (more than 200 per column blocks). CIS holds no
    traveller data by design (only staff/alert e-mails are tagged in `shared`), so the traveller DRY RUN is a proof
    of absence (exact hash on the tagged columns, hashed e-mail tokens in notification/job/audit text).
    **Apply** is in the code but OFF: it needs `ERASURE_APPLY_ENABLED=true` (unset everywhere), a verified Cognito
    staff session, a succeeded DRY RUN of the same subject <= `ERASURE_DRY_RUN_MAX_AGE_HOURS` (24) old whose
    fingerprint is passed as `approved_fingerprint`, `approved_by`, `snapshot_id`, and for a tenant an offboard at
    least `ERASURE_TENANT_RETENTION_DAYS` (default 30, Nghiệp sets it) ago. One transaction: recount = same
    fingerprint, every statement touches exactly the approved rows, a recount finds 0, one `acp_shared.audit_log`
    row (`tenant.erased` / `dsar.cis_erased`, counts only); for a traveller the hash is then stripped from every
    `data_erasure` row holding it. Inside the transaction the tenant row is locked (`FOR UPDATE`) and its
    offboarded state and retention window re-checked (a restore in between stops the apply). An apply claims its
    DRY RUN (`result.used_by_apply`, single use) and is never retried: `POST /admin/job-runner/jobs/{id}/retry`
    refuses it (409, `JobKind.retry_refusal`). A traveller job that fails or is cancelled has its hash replaced
    (`on_terminal`); `verify: true` on a traveller DRY RUN (after an apply) strips its own hash; Sentry events of
    this kind carry no frame locals (since AA-941 no Sentry event does, `shared/sentry_init.py`). Inventory and settings:
    `GET /admin/job-runner/erasure`.
  - **Report core (AA-1003 R1, root AA-1001 report catalog)**: the registry of every recurring report and KPI is
    root `docs/reports/catalog.yaml` + `kpis.yaml` (linted there by `tools/docs/report_registry.py`); this repo reads a
    byte copy in `services/reports/registry/` (`registry.py`, loaded once; change root first, then copy). Two expand
    tables (CIS 305): `shared.kpi_daily` (one value per KPI ID, ICT day and scope `all` / `app:<app>` /
    `provider:<provider>`) and `shared.report_run` (one row per generation: period, status queued → running →
    succeeded / failed, summary, output JSON, output_markdown; one scheduled run per report and period). Background
    kinds in `services/jobs/report_jobs.py`: `kpi_snapshot_daily` computes the R1 KPIs from CIS tables only
    (`services/reports/kpis.py`: K-JOB-FAIL, K-JOB-QUEUEAGE from `shared.job`; K-LLM-COST-DAY by app and provider and
    K-LLM-FALLBACK-24H from `shared.llm_call_log`; K-CAT-LIVE from `gold_aa_internal.v_tour_liveness`, latest day
    only; K-RPT-FAILED from `shared.report_run`), writes what it could and raises when a part failed (retry), and a
    scheduled run chains `report_generate` for the CIS-generated daily/live reports (EX-04 scorecard, EX-06
    register; `services/reports/generate.py`, Markdown + JSON into the run row, no S3 yet). The api's ops metrics
    sampler queues the snapshot once a day for the day `periods.snapshot_day` names (day D from 00:30 UTC of D + 1;
    days are ICT, UTC+7). Manual runs and the register: `/admin/reports` (Platform › Overview › Reports, admin;
    `GET /admin/reports`, `GET /admin/reports/{id}/runs`, `GET /admin/reports/runs/{run_id}`,
    `POST /admin/reports/{id}/run`, 409 for a report not generated here; `POST /admin/reports/snapshots` {day?}
    re-computes one day's snapshot, the back-fill path, 90 days max). $0: DB reads only.
  - **Daily safety net (AA-1003 R2)**: `report_generate` also renders **OPS-12** (daily platform health digest,
    `services/reports/digest.py`: live /health + /health/ready of CIS (public origin `PUBLIC_API_BASE`, Dev default),
    Booking and the CRM (`platform_apps.http_health_card`), CIS workers' commits vs the API's (not compared when the
    API's commit is unknown), alarms in ALARM (DescribeAlarms), failed / overdue / lease-expired `shared.job` rows, LLM
    availability (`api.services.llm_availability`; `unknown` with no answered text call in 24 h is an attention
    item, never green), nightly probe verdicts, LLM spend vs the 14-day median, worker
    starts and migrations of the last 24 h, open waves, overdue reports; every section isolated, short when green) and
    **TP-03** (inquiry handoff, `services/reports/handoff.py`: TripPlanner `inquiry_sent` vs Booking
    `inquiry_received` per UTC day from the ops.overview.v1 contract views only). Kind `inquiry_handoff_check`
    (queued hourly by the ops sampler, key per UTC hour) checks the last 7 UTC days; a gap seen at two checks in a
    row raises the admin bell `platform.inquiry_handoff.gap` (24 h throttle unless it grows) and one ops alert e-mail
    per UTC day (template `inquiry_handoff_gap`); worker variable `INQUIRY_HANDOFF_ALERTS=off` turns bell and e-mail
    off (the check still runs). Kind `report_email` sends a scheduled OPS-12 run to its opt-in list
    only (`OPS_DIGEST_EMAIL_TO`, comma-separated; empty = skipped), never to the alert recipients. KPI computers
    `inquiry_handoff` (K-TP-HANDOFFGAP, K-BK-PARKED, K-BK-INQ2CASE, UTC day D) and `nightly_probes` (DATA-01:
    K-DATA-PROBES, K-DATA-LIVENESS, K-DATA-STALE from the `SCRIPT verdict` lines of the ECS run-script probes in
    CloudWatch Logs, `services/reports/probes.py`; needs `logs:FilterLogEvents` + `logs:DescribeLogStreams` on the
    log group; the ops overview client's short timeouts; the day's migrator streams are listed (DescribeLogStreams
    newest first, back to the day's start) and searched by name over the whole ICT day, because GitHub runs the
    nightly crons hours late; slices around each cron time are the fallback when listing is refused or the day is too
    far back; all searches of a day share a 12 s budget, an unfinished search counts nothing). A computer
    whose outside source is unreadable (probe log, or a contract view revoked / dropped / reshaped) raises
    `kpis.Unavailable`: no value, reason under `unavailable` in the job result, the snapshot still succeeds and chains. $0 (view reads, two cheap AWS reads, SES).
  - **Money reports (AA-1003 R3)**: `report_generate` also renders the weekly **EX-01** (business pack draft) and
    **FI-02** (spend and vendor balance digest, e-mailed to the `OPS_DIGEST_EMAIL_TO` list like OPS-12) for an ISO week,
    and the monthly **FI-01** (cost review draft, template cost-finops-review Part A) with **FI-03** (LLM
    attribution), **FI-04** (vendors), **FI-05** (unit costs), **FI-06** (budgets) and **OPS-08** (capacity) for a
    calendar month (`services/reports/money.py`, readers in `services/reports/money_sources.py`). The daily chain
    queues them: the snapshot of a Sunday chains the weekly reports for that ISO week, the snapshot of the day before
    business day 2 chains the monthly ones for the previous month (`periods.scheduled_period`; the register's due rule
    follows the same schedule). Stored rows only, per UTC day: the LLM ledger (`shared.llm_call_daily` +
    `shared.llm_call_log`), the vendor ledgers, the STORED Cost Explorer snapshots through `platform_cost.cost_by_app`
    (`refresh_if_stale=False`) / `cost_ops.cost_forecast` / `cost_explorer.read_cost_range` (a month's AWS data is
    labelled missing / partial / provisional / final; a report never calls Cost Explorer), budget bells, the
    contract views; OPS-08 adds catalog sizes and one CloudWatch GetMetricData call for the RDS metrics (already
    granted; unreadable = named, not failed). Per-App budgets mirror Terraform in `cost_ops.APP_MONTHLY_BUDGET_USD`
    (env `COST_OPS_APP_BUDGETS_USD`); the LLM account credit is set by hand in env `COST_OPS_CREDITS_USD`
    (`acc3=<usd>@<expiry>`). A source that cannot be read is a line in the report ("not wired" / "not readable"),
    never a failed run. No migration. K-UNIT-TOUR counts tours published or republished (published_at or updated_at
    in the month, CIS 273); EX-01 falls back to the live v_tour_liveness count when the week has no K-CAT-LIVE snapshot.
  - **Quality, reliability and data reports (AA-1003 R4)**: `report_generate` also renders the weekly **CIS-01**
    (catalog health: liveness, lifecycle events, S1 writer / cover / photo coverage per country from the data coverage
    live rule and `shared.v_tour_photos`, DATA-01 snapshots, action list), **OPS-13** (C1.2 objectives CIS can measure,
    `pg_stat_statements` per role, CloudWatch DatabaseConnections, two-week misses vs the previous run; draft),
    **OPS-14** (`shared.job` per kind vs the registry's `expected_seconds`, overdue, dead scheduled kinds, cost per run)
    and **DATA-02** (handoff roll-up, `booking.v_ops_inquiry_inbox` age, `tripplanner.v_ops_inquiry_keys` unmatched
    after 24 h, LLM ledger vs the stored acc3 bill), the monthly **DATA-04** (rows past each retention window counted
    like the retention job, retention runs, growth, largest relations vs the previous run) and the event reports
    **CIS-06** (eval runs, writing-memory releases, worst items), **CIS-07** (rerun waves through
    `services/waves/report.py`, cost per stage, retry waste, projection) and **DATA-03** (`shared.schema_versions`
    per owner); event reports run on "Generate now" over the last 7 closed days (`periods.default_period`).
    `services/reports/quality.py`, `services/reports/reliability.py`. Booking / TripPlanner / CRM job tables, the
    CRM feed, GitHub checks and Vercel are named "not wired" / "not readable from CIS". No migration.
  - **R4 part 2 reports (AA-1003)**: the weekly **TP-01** (TripPlanner funnel from `tripplanner.v_ops_traffic_daily`,
    AI searches and the TripPlanner online-stage LLM spend vs the mirrored default cap from `shared.llm_call_log`;
    `services/reports/planner.py`) and **DEV-03** (CIS deploys = worker starts on a new commit in `shared.job_worker`,
    rollbacks, migrations per owner; kept in the output so the monthly **DEV-01** can count a month after the worker
    rows are pruned), the monthly **SEC-08** (audit hash chain re-verified with `platform_audit.verify_chain`, staff
    directory changes, calls by type, refused calls, off-hours bulk) and **SEC-03** (draft: staff account hygiene,
    audit chain, DATA-01 snapshots) (`services/reports/security.py`), and the event **OPS-15** (state at generation:
    ECS services via DescribeServices / DescribeTaskDefinition, already granted, image tags without the registry host,
    health, worker parity, newest migration per owner, active CIS jobs, open waves; the output is the session-close
    state-snapshot.json) (`services/reports/delivery.py`). Linear, GitHub (Dependabot, secret scanning, PRs, CI runs,
    releases), Vercel, Lambda and other owners' job and audit tables are listed as "Source not available". No migration.
  - **Ops metrics sampler (AA-832, AA-751)**: `api/core/ops_metrics_sampler.py`, a background task of
    the **api** (not the worker) started in `api/main.py`'s lifespan after the main pool, cancelled
    before it closes, off when `ENVIRONMENT=test`, never raises. Every 300 s (first tick 30 s after
    start) it makes one PutMetricData per namespace (boto3 in a thread, connect 3 s / read 5 s, one
    attempt) and logs one `ops_metrics_sampled` line with the values (`rds_error` / `retired_error` /
    `jobs_error` / `alert_recipients_error` when a part fails; the other parts still run). AA-913: one part
    reconciles the platform alert recipients with SNS (`shared.alert_recipients.periodic_reconcile`, no-op when
    the alerts topic is not configured). Each part runs under
    `asyncio.wait_for(PART_TIMEOUT_S = 60)` (the main pool has no `command_timeout`), so a hung query
    is a `TimeoutError` in the line and the next tick retries. Before the backlog sample it cancels
    queued/running rows of `shared.jobs.registry.RETIRED_KINDS` (kinds whose handler was removed;
    `ops_rds_role_connections` today, error `kind removed (AA-751)`, `retired_cancelled` in the line),
    so a rolling-deploy leftover neither pages as starved nor blocks the deploy gate:
    - `AA/RDS`, dimension `Role`: `RoleConnections` (Count) + `RoleConnectionLimitPct` (Percent) for
      every LOGIN role with a `CONNECTION LIMIT` (`pg_roles` LEFT JOIN `pg_stat_activity`, discovered
      each tick; the main pool logs in as `cis_app` since X1 S18, and `pg_stat_activity` still lists every
      role's sessions to it, only their query/state columns are hidden);
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
    guard `GET /admin/job-runner/deploy-gate` leaves such kinds out of its info counts and lists them in
    `exempt_kinds`; since AA-993 G5 those counts no longer block); only the test-only kind `noop_sleep` uses it
    (AA-993 G1, below). AA-990: the info counts also set apart queued rows due more than 1 h ahead
    (`queue.DEPLOY_GATE_HORIZON_S`, reported as `scheduled_later`). Today that is only the `place_geocode` run
    queued for the next Geoapify credit reset.
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
  - Drain on shutdown (AA-993 G1, ADR 0016 D1; root design `docs/audits/2026-10-26-S252-aa993-delivery-pipeline/`).
    On SIGTERM (`worker.serve()`) the service worker stops claiming, gives running jobs `JOB_WORKER_GRACE_SECONDS`
    (`worker_grace_seconds()`; AA-CIS-Infra sets 100 on `aa-cis-dev-worker`; absent = 20 s as before) and then
    releases the rest. The window must leave 15 s of the container's ECS stopTimeout
    (`JOB_WORKER_STOP_TIMEOUT_SECONDS`, default 120); otherwise the worker refuses to start (ValueError). The in-API
    worker keeps the 20 s class default. Logs: `job_worker_draining` (running, kinds, grace), `job_worker_drained`
    (finished / released), `job_released_on_shutdown` (next_status, `counted`, `release_delay_seconds`).
    - A release while a worker of a newer task revision is alive (a deploy, `queue.newer_worker_alive`) does not count
      toward `queue.MAX_RELEASES` (`queue.release(count_release=False)`), so deploys never fail a job; with no newer
      worker (or the check failing) it counts, as before.
    - Every release appends `{at, counted, worker}` to `progress.release_log` (last 20) and bumps
      `progress.shutdown_releases`; `progress.releases` stays the MAX_RELEASES counter. JSON in `progress`, not a
      column: `shared.job` is a hot table and the claim uses `RETURNING *` (design critic 6).
      `GET /admin/job-runner/released?since=` (default 24 h; machine GET in `MACHINE_ROUTES`, a row in
      `.github/verify/cis.rows`) returns `released`, `counted`, `jobs`, `by_kind`.
    - Registry fields: `release_delay_seconds` (a released job is due that much later; for kinds whose thread
      outlives the cancelled handler, set to the stopTimeout; `a3_atomize` = 120 since G3) and
      `deploy_blocking` (a kind a deploy must not interrupt: the deploy gate is unsafe while one of its rows runs or
      is queued due within 15 min, G5; today `data_erasure` and `s1_batch_ingest`; it cannot be combined with `deploy_gate_exempt`). `GET
      /summary` lists both per kind.
    - **Deploy gate (AA-993 G5, design D1.4).** `GET /admin/job-runner/deploy-gate` (`worker.deploy_gate_status`,
      also the Platform Apps card) answers `safe_to_deploy = false` only while a rerun wave is open (`open_waves`,
      `shared.rerun_wave.status = 'open'`) or a `deploy_blocking` kind runs (`blocking_running` per kind) or is
      queued due within `queue.DEPLOY_BLOCKING_HORIZON_S` = 15 min (`blocking_due`: the guard runs before the image
      build and the old task would claim it before its SIGTERM); `blocked_by` names the reasons. Queued/running jobs of other kinds no longer block: the worker drains them
      and the new revision re-claims a released job. `active` / `by_status` / `scheduled_later` stay as info.
    - `noop_sleep` (`services/jobs/noop_sleep_job.py`): a test-only kind, registered only when `JOB_TEST_KINDS=1`
      (set it on api **and** worker, or the post-deploy verify's worker-kinds row goes red); sleeps
      `payload.seconds` (1..3600), resumes from `progress.slept_seconds`, `max_attempts=1`, `deploy_gate_exempt`.
      It is the live drain proof: enqueue it, deploy, read the logs and `/released`.
    - Re-run safe kinds (AA-993 G2/G3). `s1_rewrite` records `progress.persist_stage` after each step
      (`services/content_generation/s1_checkpoint.py`: `writer_done` → `rewritten` → `content_inserted` → `scored` →
      `fix_pass_saved` → `routed` → `accounted`) and a re-claimed job resumes there: after the writer it re-enters the
      graph at validate (`build_graph_resumed`, the saved writer-state delta, v2 stamp kept — not the Bedrock Batch
      seed, which is v1-only); after the rewrite it pays nothing again; one version (found again by
      `generated_content.metadata.s1_job_id`), one `quality_scores` row (NOT EXISTS guard), one review entry. The early
      return is only `phase = "done"`. A real failure does not keep the checkpoint: a version INSERT data error drops
      the saved rewrite (next internal attempt rewrites) and a failed job clears `persist_stage` + the bulky keys, so a
      Jobs-page Retry rewrites; only a release (cancel) resumes. The polled Jobs list strips `s1_writer_state` /
      `s1_rewrite_result` from `progress`. `a3_atomize`: a `threading.Event` set when the handler is cancelled stops the
      thread at the next day boundary / between later steps (`AtomizeStopped`, late progress writes dropped), the
      chain runs under a session advisory lock on the tour (`handler.a3_tour_lock`, own connection, two int4 keys
      `hashtext('a3_atomize'), hashtext(tour_id)`; a waiting run polls, honours its own stop, gives up after 1800 s),
      and `release_delay_seconds = 120`; a `force` job records `progress.force_since` (first attempt) and a later
      attempt skips days whose fingerprint matches and `atomized_at >= force_since` (already re-read by this job).
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
    an endless "Writing…". The saved version also records (admin-only, hidden from every tenant read)
    how it was written (`generation_meta`, CIS 297, AA-920) and how it was scored (`judge_breakdown`, CIS 300,
    AA-795: the kept draft's brand fit / distinct / mission / feedback, the gate and the **binding axis** that set
    the score, plus `brand_claims` — age, group-size and brand-ethos claims of the saved text the A1 source does not
    support, deterministic and report-only, `services/content_generation/t2_brand_claims.py`); the admin reads it in
    Content Quality → Tour versions. **T9 write is a job** too (`t9_write`, `content_piece.job_id`; a job that
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
    publish / atom edit) and `PLATFORM_STAGES` (landing→score→route→segment_gc, platform score+route, then the
    AA-988 orphan-Segment clean-up).
    The `recompute` job and the `a3_atomize` path (its inline segment matching) run their work
    only through `run_stages`; the thin wrappers `recompute_segment_score_route` /
    `recompute_rankings_and_routes` in `services/export/handler.py` map the stage results back to
    the Jobs-page result keys (`{segment, ranking, route}` / `{ranking, route, segment_gc}`).
  - **Segments follow a re-atomize (AA-988)**: the `segment` stage drops the member rows of its tour's dead atoms
    (soft-deleted by the re-atomize, or empty-day markers) in its write transaction; the `segment_gc` stage
    (`services/acp_contract/segment_gc.py`) deletes Segments nothing refers to any more (no member on a live atom, no
    ranking row, no alias row either side, no Subject, brand-fit cache row, current / Subject-named route or
    angle-request route list) and their dead member rows. Mode env `SEGMENT_GC_MODE` = `report` (default, counts and
    samples in the `recompute` result) | `apply` | `off`; `apply` holds (`held_volume`) above `SEGMENT_GC_MAX`
    (10,000). Lock `acp:segment_write`: every segment run holds it shared from its candidate read to its write; the
    GC takes it exclusive (30 s lock timeout → `skipped`). The `a3_atomize` path re-lands the Segments the tour had
    BEFORE its segment stage too (cache clear + recompute scope), and then runs the $0 place-card relink
    (`place_cards.relink_tour_place_cards`, `max_places=0`, result key `place_cards_relink`) before `place_extract`
    (AA-989).
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
- **Tenant Account pages (AA-761)**: `GET /v1/account/activity` (the tenant's own `acp_shared.audit_log` rows,
  newest first, `category` / `action` / `since` / `until` filters, offset paging with a total, `page_size` ≤ 100),
  `GET /v1/account/activity/export` (the same filtered set as CSV, at most 50 000 rows, a cut file says so) and
  `GET /v1/account/api-key` (masked `cis_…` key, issue date from the onboard / rotate / restore audit rows, last
  sign-in with it and last metered API call). Tenant JWT only; the id comes from the token. `audit_log` has no RLS
  and no tenant-role grant, so these read on the main pool with the id bound (the `/v1/billing` pattern), and only
  an **allowlist of actions** is visible (`api/services/tenant_account.py` `TENANT_VISIBLE_ACTIONS`): admin silent
  corrections, the staff request trail, brand / config / membership edits and the erasure record never are. A new
  audit action is invisible to tenants until it is added there. `details` never leaves raw (a per-action whitelist
  builds `detail`). Billing still reads `/v1/billing` + `/v1/quota`; CIS stores no invoices yet.
- **Pipeline routes are admin-only (AA-799)**: every `/v1/pipeline/*` route requires the admin secret or (AA-816
  C9) a verified staff principal (`_require_admin`, a router-wide dependency in `api/routers/v1_pipeline.py`); a
  tenant JWT gets 403,
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
- **Quarantine schema `retired` (data standards DM-3.9, CIS 294, AA-850)**: dead CIS tables and views wait there
  for their drop migration (>= 14 days later unless Nghiệp waives the wait) with no grant to any role (`cis_app` has
  no USAGE: a forgotten reader fails with 42501). Owner migrations only; never read or write it from code. Wave 1
  (quarantined by 294, dropped by 296 with the 14-day wait waived in S255; empty `retired` stays for the next waves):
  `acp_contract.atom_decompose_jobs`, `s1_from_atom_runs`, `route_pick`; `acp_shared.marketplace_portfolios`,
  `tenant_atom_state`; `shared.acp_runs`, `tenant_brand_rule_versions`, `tenant_brand_rules_deleted_aa404`,
  `v_batch_stats`, `v_pipeline_summary` — none of these exists any more, do not rebuild on them. The tenant erasure
  skips `retired` (`services/erasure/inventory.py` `QUARANTINE_SCHEMAS`; an FK child there is a `quarantined_child`
  information line). Registry: AA-Ecosys `docs/audits/2026-10-28-S255-aa850-dead-tables/registry.md`.
- **The ACP v1 stage schemas are gone (CIS 299, AA-850)**: `acp_gold_output`, `acp_silver_s2`, `acp_silver_s3`,
  `acp_silver_s4`, empty since 121 / 208, were dropped (no quarantine: no object to wait). The `/v1/social` router
  over `acp_silver_s4.social_content` (every call had failed since 121) went with them. Do not recreate them.
- **ACP v1 run model is gone (AA-751, 16/10)**: `acp_shared.acp_runs` was dropped by migration 121;
  its last readers (`GET /admin/acp/stuck-runs`, `POST /admin/acp/runs/{run_id}/force-fail`,
  `services/acp_shared/cost_utils.py`, `services/acp_canary/`) are deleted. Stuck or long work is a
  `shared.job` row (reaper + admin Jobs page). `tests/unit/test_aa751_no_acp_runs_reader.py` fails
  on any new SQL against `acp_runs`. `infrastructure/canary/` (old-account EventBridge skeleton) is deleted
  too: its schedule and Lambda no longer exist (checked 16/10/2026).

- **Photos (AA-708, migration 198)**: the `photo_sync` job copies the Jira CON board's PHOTOS
  Drive folders (public, read with a Google API key from `aa-cis/dev/gdrive-photo-reader`) to S3
  (`photos/<country>/<file id>-{1600,960,600}.webp` + the original) and records `shared.place_photo`, matched to a tour
  (folder name) and to a `shared.destinations` place (file name; see below). Images are public via
  `GET /content/photos/{id}?size=`, the stable path every consumer stores (catalog photo views,
  TripPlanner covers and `og:image`, Booking `CisPhoto`). AA-844: it answers a 302 to the photos CDN
  (CloudFront `img.lumiguides.it.com`, AA-CIS-Infra `photos_cdn.tf`, in front of the bucket's
  `photos/` prefix) as `<host>/<slug>/<file>?v=<sha256[:12]>`, cached 1 day; `PHOTO_CDN_BASE=off`
  falls back to a presigned S3 URL (1 h, redirect cached 50 min). The API stays in the image path
  (one hop + one row read per view); CloudFront serves the bytes. A rejected photo is 404 on the
  route, but a CDN URL handed out earlier keeps working (the object stays in S3, edge cache 1 year)
  — reject is reversible, so objects are not deleted. A daily schedule (Infra ops-probe Lambda,
  21:00 UTC = 04:00 ICT) calls `POST /admin/photos/sync/schedule`: one incremental `photo_sync` job
  per CON country, idempotent per UTC day, skipped while another `photo_sync` job is queued or
  running (not skipped during a rerun wave: no LLM work); `/admin/photos/summary` reports it as
  `last_scheduled`. (Before AA-993 G5 those serial jobs held the deploy gate around 04:00 ICT; they no longer do.)
  AA-671 S257 — **photo → destination**: a photo is compared only with the places of ITS OWN tour (the `geo.tour_place`
  rows of the tour's source key + the current graph build's stops, non-duplicate destinations;
  `services/photos/destinations.py`), and only an unambiguous answer of a strong rule is written
  (`match.resolve_destination`: exact name, core name, a place name inside the label; a tie = ambiguous; a label found
  only inside one place name — a town label vs its landmark — is `weak`: counted, never written).
  `place_photo.destination_match_source` (migration 303) says who set the destination: `manual` (the assign route) is
  never changed by the sync, the job or a folder-wide tour correction; `auto` follows its tour (emptied or re-answered
  when the tour changes). Every sync fills an empty `destination_id`; `match_destinations` additionally re-matches the
  automatic destinations of the tours whose places it read (never after a failed read), and fills empty
  `shared.destinations.cover_image_url` (read by the TripPlanner). The photos synced before get theirs from the
  `photo_destination_match` job (`POST /admin/photos/destination-match`, DRY RUN by default, background, $0: counts
  matched / ambiguous / weak / no_match / no_candidate / no_label per country with samples and previews the covers;
  an apply writes in one guarded UPDATE; covers only with `fill_covers`). `destination_id` feeds
  `shared.v_destination_photos` / `catalog.v_destination_photo` and so the pool cover chain and the TripPlanner
  destination pages. S258: the job's matcher version 2 (`{"version": 2}`; the sync stays on 1) breaks a tie between
  places on one spot (every pair ≤ 2 km: most tour days wins) and matches a label that IS the name of a place ≤ 20 km
  from a place of the tour (`nearby_exact`: the town Paro, not the monastery above it). The pool cover chain
  (`services/photos/covers.py`, both pools, also run by the job's `covers_coverage` report): tour photo → a photo of
  the primary place (most days) → of the photo place nearest to it (≤ 25 km) → of a photo place of its country.
  AA-995 (migration 301): `size=medium` is the 960 w file (`s3_key_medium`, phone heroes of the TripPlanner); a row
  without it answers the 1600 w file (never the original). The sync writes it for every file it downloads; files stored
  before 301 get it from the `photo_medium_backfill` job (`POST /admin/photos/medium-backfill`, background, $0: S3 +
  Pillow on the worker, rendered from the stored original, no Drive download; bounded by `limit`, resumes with what is
  still missing, stops while a `photo_sync` runs). `/admin/photos/summary` counts `with_medium`.
- **Place pipeline (AA-712, ADR 0007, migration 226)**: CIS — not TripPlanner — computes each live
  tour's day skeleton (start / end / overnight), places (= itinerary components; id = uuid5(source, day, itinerary spelling, country, activity) at a place's first build, and AA-989: a rebuild keeps the previous row's id for the same place respelt, retyped or moved a day — `components.carry_over_ids`, unambiguous one-to-one pairs only — because TripPlanner trips pin components by id; AA-990: a generic name ("Night Market") keys the carry-over only together with its town; every other dropped id gets a redirect to the component of the same place (destination → card → spelling + country → link name, nearest day; generic names in the same town only), written to `geo.component_redirect` (CIS 285) in the transaction of the `geo.tour_place` rewrite (ids served again are cleared) and served to TripPlanner as `catalog.v_component_redirect`, whose readers follow the chain together with `tripplanner.component_id_map`; `components_hash` includes the atom ids),
  geocoded destinations and the tour graph (stops, nodes, edges, legs, junctions) into the CIS-internal
  schema `geo`, keyed by `source_id` (catalog identity). Jobs (`services/jobs/place_*`,
  `tour_graph_job.py`; logic in `services/places/`): `place_extract` per tour (after A3 atomize — automatic only
  with `PLACE_PIPELINE_AUTO` on or, AA-989, when the tour's source already has a `geo.tour_build` row ("maintain
  what exists"; coverage expansion stays gated, S236) — set-active / restore, admin backfill `POST /admin/places/backfill`, ≤ 50 tours of a country or an explicit
  `tour_ids` list ≤ 100 (AA-847), dry-run cost estimate priced at each stage's live route and catalog price,
  refused during a wave); `place_geocode` (debounced, the ONLY writer of
  `shared.destinations` coordinates) — the model proposes a point (`a3_place_locate`), **Geoapify**
  (OpenStreetMap data; S236 decision, all countries; `GEOCODER_PROVIDER` = `geoapify` default |
  `model_only` for tests / emergency; `mapbox_permanent` / `amazon_location` fail as not supported —
  Mapbox is the basemap only, Amazon Location is not used) confirms it with the bake-off acceptance
  rule (`services/places/geomatch.py`: top 3, name match, no admin-area types, ≤ 25 km / 50 km for long
  features; `rank.confidence` recorded, never a gate) → `located_by='geoapify'`, `geo_precision='point'`.
  **AA-997 (S254 geocode audit): coordinates come from the geocoder only.** With a tour route (the located overnight
  stops of days d-1..d+1 of the tours using the name, `store.route_anchors` / `guards.route_km`, ≤ 50 km for a site,
  100 km for a long feature; a day trip ≤ 150 km off the route passes only as a second choice when the answer also
  agrees with the model's point) the route is the distance reference instead of the model's point, which is only a
  hint (Geoapify `bias`); unconfirmed → the town the model names, geocoded as a settlement (`area`,
  `review_reason='centroid_fallback'`); else no point (`unconfirmed_by_geocoder`, `unknown` at once). A model null gets
  one geocoder-only search when a route exists, else it is final. China searches cover HK / MO and a point there is
  stored as Hong Kong / Macao. Only `GEOCODER_PROVIDER=model_only` still stores the model's point (`llm`, `area`,
  `review_reason='llm_point'`). Names that are not places (route legs, generic lodging, activities, regions,
  descriptions: `services/places/name_filter.py`) never reach a paid call (`unknown`, `last_error='non_place:<cause>'`).
  Existing model-placed rows are re-located by the job kind `place_relocate` (`POST /admin/places/relocate`, dry run
  first; flags `off_route` / `llm_point`, optional hide; no model call; 1,000 daily credits kept for place_geocode).
  Its no-call modes hide flagged pins from every reader (`suspect`): `hide` by review reason, `hide_flagged` (S260)
  only rows a run already stamped `llm_point` / `off_route` and left on the model's point (report, then confirm,
  audited; a confirm hides only the rows its report listed); `unhide` reverts by stamp or by id (a restore by id
  stays shown on later hides), and a hidden row a later run confirms is shown again.
  Generic phrases ("ger camp", "local market") are not geocoded. Free plan 3,000 credits/day: ≤ 5 req/s, retry on
  429/5xx, daily credit guard 2,500 (UTC day, `geo.geocode_call`); AA-990: a run stopped by it with names still
  due queues the next run for 00:10 UTC + jitter (`enqueue.enqueue_after_credit_reset`, one row per reset day, queued
  geocode rows moved there too), and the data coverage page flags names due for > 24 h (`geo.names_stalled`). The
  locate answer is read leniently (`locate.json_object`, re-spelt keys by `name_norm`), an unreadable batch is asked
  again as two halves, and a name without a point carries its reason (`unknown_to_model` = null,
  `missing_in_answer`, `malformed_answer`, `unknown_country:<c>` = names no ISO country). Key: Secrets Manager
  `aa-cis/dev/geoapify` (plain string), worker task role reads it; never logged. **Attribution
  required wherever these coordinates are shown (CIS admin and every consumer, TripPlanner first):
  "Powered by Geoapify" (link) + "© OpenStreetMap contributors"** — also kept per row in
  `geocoder_meta.attribution` (Geoapify storage terms). Hard country/bbox guard on the PLACE's own country — any ISO
  3166-1 country (AA-990), never the tour's: a tour sold as India may climb Kilimanjaro, whose places are Tanzanian
  (cross-border is normal, not an error) — transit-aware outlier
  rule → `suspect`; admin-fixed (`manual`) rows are never overwritten; `tour_graph_build` (debounced, versioned swap, current + 2
  kept). Stages `a3_place_days` (Haiku 4.5, Sonnet fallback, migration 260), `a3_place_locate` (Sonnet, Haiku
  fallback, migration 261: on Haiku its coarser points left most names `llm`), `a3_place_embed`
  (Cohere 1536-d). `shared.destinations` is unique on `(name_norm, country, locality_norm)` (same
  name in two countries = two places). Other apps will read it only through `catalog.*` views
  (migration 228, next slice); TripPlanner's own extraction is frozen.
- **Place kinds and the content-validity gate (AA-847).** `geomatch.feature_kind` names a place that is not a
  pin: a **linear** feature (trail, river, road, loop, stream, path, circuit, expressway …) is stored
  `geo_precision='line'`, `geo_status='ok'` and DRAWN as a line (owner decision S246: never a pin, a circle or
  hidden): `place_geocode` inserts its pending `geo.place_shape` row and never moves its point for shape reasons (it
  stays the only coordinate writer); an **area** feature (national park, delta, plateau, coast, gorge, mountain
  range) is `area` even when Geoapify confirms a point. A name that names a point on the feature ("River Kwai
  Bridge", "Kuang Si Falls", "Peradeniya Railway Station") stays a point. A descriptive atom place around a feature
  ("sandbank on the Nam Ou River", "Annapurna Circuit Trek") links its component to the feature's destination
  (`geomatch.linked_feature`, labelled set in `tests/unit/test_aa847_linked_feature.py`). A line destination is never
  a graph stop or node, never in the outlier rule and never in the point-distance same-place rule.
- **Line geometry (AA-847, CIS 255).** `geo.place_shape` (one row per line destination: status pending / ok /
  approximate / not_found / failed / manual, GeoJSON at ≤ 2,000 / 300 / 60 vertices clipped to the country, non-null
  bbox, `osm_type`/`osm_id`) is written only by the job kind `place_shape` (and the admin's manual GeoJSON);
  `geo.tour_segment` (the stretch a tour-day follows, versioned with `geo.graph_build`) only by `tour_graph_build`.
  The Geoapify daily credit guard sums `geo.geocode_call.credits` of every endpoint (search + Place Details).
  `place_shape` (debounced, cap 1, background; off unless `PLACE_SHAPES_ENABLED`; `GEOCODER_PROVIDER=model_only` = no
  call) takes the geometry from Geoapify Place Details by the stored place id, then Overpass (free, ≤ 1 request / 2 s)
  when that is missing or only one OSM way; `services/places/shape.py` accepts a candidate on name (name / name:en /
  int_name / name:*), kind (waterway / road / route), the country POLYGON (Natural Earth admin-0,
  `services/places/geodata/`; not `guards.BBOX`), distance to the tour-day anchors and a kind-aware length, and
  prefers a relation or merged same-name ways over one way. No OSM feature → the anchored approximate route of a
  tour-day (status `approximate`, drawn dashed "approximate route"); no anchors either → `not_found`, retried and
  listed on `/admin/places` (Lines tab; `POST /admin/places/shapes/backfill` dry run first, ≤ 50 names;
  `PUT|DELETE /admin/places/{id}/shape` manual GeoJSON). `place_geocode` and `place_shape` share one advisory lock
  (`services/places/geolock.py`): one Geoapify client at a time. `tour_graph_build` cuts each day's stretch
  (`shape.day_segment`: between the previous and this day's stop, or ±15 km around one, or none = the whole
  feature). Two lines are the same place only on the same country + kind + name and the same OSM id or ≥ 70 % overlap
  (`shape.same_line_place`; labelled pairs `tests/unit/fixtures/aa847_line_pairs.json`).
- **Automatic duplicate merge (AA-847 Problem 2, CIS 257; owner decision S246: no manual merges as the normal
  path).** One decision `services/places/dedupe.decide(a, b)` → auto_merge / llm / keep (rules `r1.4`, pinned by
  `tests/unit/test_aa847_dedupe.py` on a labelled pair set: 0 false merges in every metadata mode). A shared geocoder
  id or place card is evidence, never a verdict (it auto-merges only POI / natural objects with exact / fuzzy names
  and no cap); two drawn lines merge on their shape overlap, a line without a drawn shape or a point vs a line is at
  most the model's call. The job kind `place_dedupe` (debounced, background, cap 1; queued by `place_geocode` when it
  wrote a destination, by `place_shape` (`enqueue.enqueue_dedupe`), by an undo, by the admin) recomputes every pair
  per country, asks the middle band of allow-listed countries to two models that must agree (`a3_place_dedupe` GPT-6
  Luna A/B and B/A, `a3_place_dedupe_check` Haiku; verdicts cached in `geo.place_pair_verdict`, budget
  bedrock/job:place_dedupe $0.50/run, ≤ 150 pairs/run) and writes only for countries in mode `apply` of
  `geo.place_dedupe_country` (the approved dry-run diff; more than max(5, 1 %) changes outside it → `held_volume` +
  alert). `place_dedupe` is the only writer of `duplicate_of` (the admin fix is an override); every merge is a
  `geo.place_merge` row (repointed rows, flattened members) and is undone by relink (`POST
  /admin/places/merges/{id}/undo`, `different` pin). place_geocode upsert / relink, the place_extract persist, merges
  and the admin fix take one advisory lock (`store.lock_destinations`). `catalog.v_destination_redirect` maps a
  merged id to its keeper (TripPlanner answers 301). `place_extract` maps a tour's in-tour duplicates with the same
  decision, rules band only (`dedupe.in_tour_remap`). `services/places/validity.py` is the deterministic report (reason codes V1 duration / V2 same place / V3
  not a pin / V4 day text; V2 is `dedupe.decide`: `near_duplicate` = an auto_merge pair not merged yet,
  `possible_duplicate` = a model-band pair, informational), run after every
  `tour_graph_build` (counts in `geo.graph_build.stats.validity`) and on `/admin/places` (Coverage + Content
  validity tabs; `GET /admin/places/validity|coverage`, `POST /admin/places/validity/fix` dry run by default, apply
  refused with 409 when the plan changed since the dry run's `fingerprint`, audit row with the prior values,
  `undo=<audit id>`). The **flip gate** (`validity.GATE_REASONS`, `summary.gate`) counts only map defects the fixes
  clear: `near_duplicate`, `linear_feature` (a linear name not stored as a line), `area_feature`,
  `linear_without_shape` (a line whose shape is not ok / manual / approximate); `same_destination_names` (one pin,
  several spellings),
  V1 and V4 are informational. A duplicate merge sets `duplicate_of` and repoints the CIS rows
  (`geo.tour_place`, `geo.tour_day`, `geo.place_name`, `acp_shared.place_card`, `shared.place_photo`); Booking and
  TripPlanner rows are reported, never written; `store.known_destinations` / `upsert_destination` answer a merged
  key with its keeper, so a rebuild does not recreate the duplicate.

## Deploy & CI

- **Backend (ECS)**: shipped via CI; a new task definition rolls out on the `aa-cis-dev-api`
  service. This repo has **branch protection with 5 required CI jobs** — merge PRs with
  `gh pr merge --auto` (auto-merge once checks pass). Agents must not push to `main` directly.
- **Post-deploy verify (AA-993 V1, ADR 0016 D3)**: `deploy-dev.yml` calls `post-deploy-verify.yml` after `deploy`
  (also dispatchable with `expected_sha`; its own concurrency group, never `deploy-dev-cis`). Script
  `.github/scripts/post_deploy_verify.sh`, HTTP rows in `.github/verify/cis.rows` (a PR that adds an endpoint worth
  guarding adds a row). Red, never a warning: api/worker rollout not COMPLETED, desired 0, an image other than
  `dev-<sha>`, `/health` git_sha/version or `/health/ready` (required schema of the commit) off, an admin row failing
  after 3 tries, no live worker on the SHA serving every registered kind, or `ADMIN_SECRET` missing. The job
  **Deploy Dev result** is red when any job (verify included) did not succeed, skipped included.
- **CI gate parity (AA-879)**: `ci.yml` also runs **Frontend Build** (`tsc --noEmit` + `next build` of `frontend/`,
  no secret needed; a required check). The **Portal Build** / **Portal Lint** jobs went with the frozen `portal/`
  copy at the AA-979 P2b clean-up (Portal Build was also removed from the required checks).
- **Secret scan (AA-981 follow-up)**: `secret-scan.yml`, check **gitleaks** on every PR (lines the PR adds, rules
  and baseline from the base branch; a change to `.gitleaks.toml` / `.gitleaksignore` / either secret scan workflow /
  `.github/CODEOWNERS` / any `.gitattributes`, or a new inline `gitleaks:allow`, needs the label `secret-scan-baseline-reviewed`, added after the last push: any later
  push, reopen or label change removes it) and **gitleaks full history** (`secret-scan-history.yml`) nightly / on push to `main`
  (branches, tags and pull request heads; merge commits, renames and type changes included, every file read as
  text; historical findings pinned by commit in `.gitleaksignore`). A red result means the value is public: rotate
  it, even if it was force-pushed away.
  The PR check runs on `pull_request_target`: GitHub always runs the base branch's copy of the workflow and the PR
  head is only read as data, so a PR cannot change the check that judges it. It also scans a squash preview of the PR
  (what the squash merge writes), and it fails when another workflow has a job named `gitleaks`. `.gitleaks.toml`
  carries a copy of the gitleaks v8.30.1 default rules and no path allowlist, so every file is scanned (`.svg`,
  `go.sum`, `node_modules/`, images, lock files); UTF-16 files are transcoded to UTF-8 and scanned too.
  The image scan in **Docker Build Check** is blocking: Trivy fails on any fixable CRITICAL/HIGH finding
  (`ignore-unfixed`); an accepted one goes into `.trivyignore` with a `# reason` line and `exp:YYYY-MM-DD`
  ≤ 90 days ahead (`.github/scripts/check_trivyignore.py`). **Unit Tests** enforces a coverage ratchet
  (`.github/coverage-ratchet.json`, `.github/scripts/coverage_ratchet.py`): below `floor` fails, lowering
  `floor` fails, below the monthly goal (+5 points a month up to `cap`) only warns. `deploy-dev.yml` does
  not trigger on `frontend/**` (the image has no frontend file). Security headers on previews are checked
  by the UI smoke specs (`aa876-security-headers.spec.ts`, `aa885-portal-host.spec.ts`).
- **Public repo, no resource IDs (AA-902)**: the required **Lint** job runs
  `.github/scripts/check_resource_ids.py` over every tracked file and fails on an AWS account ID, an ARN
  with an account, an `<name>-<account>` resource name, a VPC resource ID or an API Gateway host (AWS
  documentation example accounts allowed for test fakes). Exceptions only in
  `.github/resource-id-allowlist.txt`, each with a max count and an expiry date. Code gets the bronze
  bucket from `shared/aws_client/bronze.py` (`BRONZE_BUCKET`, else `aa-cis-bronze-<caller account>`) and
  acc2's own ID from STS; the acc3 satellite ID comes from the task env (`BEDROCK_ACCT_PRIMARY`), read once in
  `shared/llm_client/bedrock_satellite.py`.
- **No acc1 in runtime code (AA-998)**: `tests/unit/test_aa998_no_acc1_runtime.py` fails when `acc1`, its account
  ID, its IAM roles, its account env var or its CLI profile appears in api/ (not migrations), services/,
  shared/, worker/, scripts/ or frontend/ — comments included.
- **Quality registry lint (AA-926)**: the required **Lint** job also runs `python -m services.quality.lint --report`
  (PyYAML only): no gate-name or 7.0 pass-line literal outside `services/quality/` (api/, services/, shared/,
  worker/, scripts/aa729/ and the frontend/portal TS; docstrings, comments, `__all__`, tests and migrations
  exempt; escape `# qlint: allow <reason>`, counted), every gate / chip / Jev key / A1 code mapped to a class
  (incl. the day-oracle, S1-first code-check and degraded codes in `services/quality/`), every Q0 corpus case's
  `class_id` and `detector` resolving through the registry (a gating Q0 case on a `warn`-only class is a warning,
  AA-924 input), the three YAML schemas, every `paths.yaml` glob matching a file, the generated TS fresh. The
  non-required job
  **Quality Paths (report)** prints the quality tiers a change touches and a hash of its diff on
  `services/quality/paths.yaml` paths (AA-919 will enforce `quality/q2` with it).
- **Frontend (Vercel)**: deploys are created by CI, not the Vercel Git integration (S241 — Hobby
  cap ~100 deployments / 24 h for the whole team, canceled ones included).
  `frontend/vercel.json` sets `git.deploymentEnabled: false`; `.github/workflows/vercel-deploy.yml`
  runs `vercel deploy` (CLI pinned) from the repo root only when a PR or a push to `main` touches
  `frontend/**`: PR → preview of the head commit + one PR comment with the URL, then the UI smoke;
  `main` → production (+ a 200 check with the bypass header); `workflow_dispatch` → preview or
  production (production only from `main`). Needs the repo secret `VERCEL_TOKEN` (team-scoped);
  without it the job fails red on every target (nothing else deploys the frontend). A PR
  that touches no frontend file gets no preview. The check to look at is "Vercel deploy".
- **Portal**: no workflow here deploys, builds or tests the portal. It deploys from AA-Partner (`deploy-portal-dev.yml`,
  ECS `aa-crm-dev-portal` behind CloudFront since the P2b cutover); its Vercel project was deleted at the P2b
  clean-up (AA-979, runbook §4.5).
- **UI smoke (AA-732, `.github/workflows/ui-smoke.yml`)**: called by `vercel-deploy.yml` after a
  successful deploy (`workflow_call`, inputs `base_url` + `sha` + `staff_session`), or run by hand
  (`workflow_dispatch`); Vercel's `deployment_status` no longer arrives. A Playwright suite (`tests/e2e/smoke/`)
  opens the key admin pages and fails the check on any HTTP/console error, failing same-origin `/api/` call, or a
  blank page (no table rows and no kit empty state). **Split (AA-816 S2 path):** specs that need a session carry
  the tag `@staff` (project `smoke`, after `smoke-setup`); every other spec is project `smoke-public` (no session).
  A per-PR preview (`*.vercel.app`, not a Cognito callback) runs only `smoke-public` (job `smoke`). The signed-in
  run (job `smoke-staff`, `staff_session=true`, from `main` only: GitHub environment `e2e`, the read-only mailbox
  role, concurrency group `e2e-cognito` shared with the Cognito E2E) signs in as the Cognito E2E user with an email
  code (`tests/e2e/helpers/cognitoSignIn.ts`, shared with `tests/e2e/cognito/`) on the two callback hosts only:
  Production after every production deploy (job `smoke-production`; the user is a reviewer, so reviewer mode:
  pages from `frontend/lib/rbac.ts` `PAGE_RULES`, no writes) and the fixed Preview host after an alias-only
  dispatch (job `smoke-preview-host`; viewer mode, and the setup fails unless the session's role is `viewer`, the
  live evidence of the Preview client cap). For a PR labelled `preview-signin`, dispatch `ui-smoke.yml` from `main`
  with `base_url=https://aa-cis-preview.lumiguides.it.com` and `staff_session=true`. Screenshots (desktop 1440 +
  mobile 390, light + dark) upload as an artifact (the public run also uploads the HTML report; the signed-in run
  never does: a `fill` step would name the code) and link from one PR comment each. Secret:
  `VERCEL_AUTOMATION_BYPASS_SECRET` (preview SSO bypass), sent only to the BASE_URL origin (origin-scoped route),
  never to a third-party host. The `E2E_ADMIN_*` password secrets are no longer passed, and since S2 the smoke setup
  has no password mode (locally, run `--project=smoke-public`; agents have no mailbox access). The admin suite also checks that the old portal URLs answer 308
  to the portal host (`aa885-portal-redirect.spec.ts`). Not a required check.
- **Portal UI smoke**: moved to AA-Partner with the portal (AA-979: `e2e/smoke-portal/`, `ui-smoke-portal.yml`).
  e2e specs carry no hardcoded credentials (shared helper
  `tests/e2e/helpers/auth.ts` reads the env). ESLint enforces UI-kit conventions in
  `frontend/eslint.config.mjs` (`frontend-lint` CI job, the kit preset at `error` with a shrinking suppressions file
  since AA-994 S1c, see "Kit lint").
- **Cognito E2E (AA-816 E1, `.github/workflows/e2e-cognito.yml`)**: `workflow_dispatch` on `main` only,
  GitHub environment `e2e`, OIDC role from secret `E2E_MAIL_ROLE_ARN` (Infra `aa-cis-dev-e2e-mail-reader`,
  read-only: List + Get on `s3://aa-cis-dev-e2e-mail-<acc2>/inbound/`, no delete; 1-day expiry). Playwright
  project `cognito` (`tests/e2e/cognito/`, always the production host — the only `cis-admin` callback; the fixed
  Preview host belongs to the separate preview client and is covered by the signed-in UI smoke): staff
  user "E2E Cognito" (`e2e@e2e-mail.lumiguides.it.com` — the SES test mailbox itself, reviewer, Booking none; a
  TEMPORARY exception to the `@adventure.asia` rule, see Staff roles) signs in by email code (`tests/e2e/helpers/mailbox.ts` reads the code from S3), lands under `/admin`,
  then (AA-680 staff-half evidence) opens the Booking staff admin (input `booking_base_url`: production
  `https://booking.lumiguides.it.com` by default, or the fixed Preview host `https://booking-preview.lumiguides.it.com`,
  AA-911; optional `e2e` secret `BOOKING_PREVIEW_BYPASS` sent as headers to that host only) and signs in
  with company email on the shared managed-login session — Booking must refuse it (`/login?reason=no_booking_role`,
  no `bk_*` session cookie, `/` still bounces to `/login`; screenshots `04b`–`04d`) — goes back to CIS, signs out,
  no silent re-login (the next sign-in shows the address form and then sends a new code, which is never typed). The sign-in test skips unless `E2ECOGNITO=1` (set only by the workflow); only screenshots
  are uploaded (reports would carry the typed code). Not on any PR.
- **Migrations are not applied by Deploy Dev.** Deploy only builds the image and updates the ECS
  task definitions. Migrations are applied only through the AA-CIS-Infra **DB Migrate** workflow
  (`check` → `apply`) from a byte-identical copy in `db/migrations/cis/` (S225); never by hand. A
  migration that the new code needs is applied before that code merges.
- **`catalog.*` view changes use the X1 S22 catalog recipe** (AA-852 / AA-860, X1 plan S22 / XD9,
  `AA-Ecosys/docs/audits/2026-10-17-S241-x1/x1-plan.md`). The views belong to the NOLOGIN role
  `catalog_owner`; since 245 schema `catalog` belongs to `cis_owner` and `catalog_owner` only has USAGE on it, so
  `CREATE OR REPLACE VIEW catalog.*` fails both as `cis_owner` ("must be owner") and as plain `catalog_owner`
  ("permission denied for schema catalog"). A migration here changes a catalog view only like this, inside its one
  `BEGIN … COMMIT` (DB Migrate already runs the file as `cis_owner`):
  `GRANT CREATE ON SCHEMA catalog TO catalog_owner; SET LOCAL ROLE catalog_owner; CREATE OR REPLACE
  VIEW catalog.v_… AS …; SET LOCAL ROLE cis_owner; REVOKE CREATE ON SCHEMA catalog FROM catalog_owner;`
  **Never `RESET ROLE`**: it returns to the DB Migrate login `db_migrator`, which inherits CREATE on every CIS
  schema through `cis_owner`, so what is created after it is `db_migrator`'s (outside the `FOR ROLE cis_owner`
  default privileges: `cis_app` gets 42501); the AA-CIS-Infra role lint refuses it and DB Migrate's ownership
  post-check rolls such a file back (exit 9). The recipe needs a DB Migrate login that is a member of
  `catalog_owner`: `db_migrator` (from X1 S22; Infra rl-002), never the RDS master (not a member since 225 and must
  never become one: the `catalog_owner_aa_master_read` policies would reach its members). Example and CI proof:
  `tests/integration/test_aa860_migration_246.py`. The old 225/235 `GRANT catalog_owner TO CURRENT_USER` dance stays
  refused by the role lint for files ≥ 243. **The XD9 freeze** (no `catalog.*` view change from the X1 S1 merge until
  S22, because the master was the login) **ends when 246 is applied on Dev** (DB Migrate logs `ran as: cis_owner`
  with login `db_migrator`); if the migrator task definition is ever rolled back to the master, DB Migrate refuses a
  recipe file again at `check` (exit 8).
- **Catalog L4 read model (CIS 253, AA-847; the first view change under the recipe above).** `catalog._live_tour`
  (internal, not barrier, not granted) is the one definition of a live AA tour (225's `live` CTE, whole projection);
  `v_tour` is built on it and every place / graph view joins it instead of `v_tour`, so a graph read no longer
  computes `v_tour`'s photo count, cover, duration regex and md5 per tour. `catalog._live_stop` (current build ⋈ live
  ⋈ usable destination) feeds `v_tour_stop`, `v_graph_node` and `v_tour_junction`. `catalog.v_component_embedding`
  (pgvector only, `tripplanner` only) serves the HNSW top-k over the components `v_tour_component` shows (live tour,
  usable place: never a suspect or merged one), so the reader checks nothing after the scan. Columns and rows of
  every contract view are unchanged (fingerprint test).
- **Line places in the catalog (CIS 256, AA-847; contracts `catalog.tour.v1.2`, `catalog.graph.v1.2`).** Columns only
  appended: `v_destination` gains `shape_kind`, `shape_status`, `shape_source`, `length_km`, never-null `min_lat` /
  `min_lng` / `max_lat` / `max_lng` (a line's shape bbox, else the point: map tiles filter by bbox intersection with
  plain float comparisons, pushed below the barrier; a read without photo columns plans no photo LATERAL) and
  `shape_lo`; `v_tour_place` and `v_tour_component` gain the current build's day stretch (`segment`,
  `segment_basis`, `segment_from_destination_id`, `segment_to_destination_id`, `segment_km`; the component view also
  `precision`); `catalog.v_destination_shape` (tripplanner only) serves a line's `shape_mid` / `shape_lo` by id;
  `_live_stop` never shows a line. Tests apply 253 + 256 inside one rolled-back transaction
  (`tests/integration/test_aa847_migration_256.py`, with the EXPLAIN checks in the consumer's filter shape).
- **Countries (CIS 279, AA-851; data standards X5 / DM-2.4 / NAM-11; contract root `catalog.country.v1`).**
  `shared.countries` is the one ISO 3166-1 table of the ecosystem (249 rows, key `iso2`; `name` = the common English
  name AA writes, `iso_name`, `aliases`). `shared.country_iso2(text)` is the one free-text resolver (same answers as
  `shared/countries.resolve` for every lookup key; test_aa990_migration_288 pins it). `raw_tours` and
  `shared.destinations` carry `country_iso2` (FK, filled by the trigger `shared.set_country_iso2()` from `country`
  for every writer; an explicit value is kept; unknown → NULL, listed by `shared.v_country_unmapped`). A role that
  writes either table needs SELECT on `shared.countries` and EXECUTE on the resolver (the trigger runs as the writer,
  MIG-6; 279 grants both to every writer it sees, on Dev `cis_app`, and refuses a writer without them); other apps
  read `catalog.v_country` and `catalog.v_destination.country_iso2` (booking, tripplanner). New country columns are
  `country_iso2 char(2)` keyed to `shared.countries`, never a new free-text key.
  **AA-990 (CIS 288, S252): no hand-made "AA country" list.** `raw_tours.country` is a foreign key to
  `shared.countries.name` (the 20-name CHECK `chk_raw_tours_country` is gone) and `(country IS NULL) = (country_iso2 IS
  NULL)`. A tour's country is exactly what its source says: S0 (`shared/country_resolver.py`) takes the Excel column,
  the file name only when the column is empty, never the file name over a different column value, never a Jev guess
  and never a re-label from the itinerary. Each place gets its own real country from geocoding
  (`shared.destinations.country`). The code reads every country from `shared/countries` — the in-code mirror of
  `shared.countries` (`countries.json`, generated by `scripts/aa990_country_data.py` from 279 + 288 with Natural
  Earth bounding boxes and tzdata zones; drift tests `tests/unit/test_aa990_country_mirror.py` and
  `tests/integration/test_aa990_migration_288.py`): the place guards, the polygons (`services/places/geodata`, every
  ISO country, parsed per country on first use), Geoapify's `countrycode`, catalog.http.v1 codes and zones, AA content
  `applies_to.country_codes`, the SEO meta country words. A compound place-card country ("India and Pakistan") is never
  stored on a new card (`place_cards.normalise_card_country`); old compounds stay inside identity keys. What stays
  AA-specific on purpose: SEO buyer markets, `booking.countries`, `geo.place_dedupe_country` approvals, brand rules, the
  hand-tuned zone splits and bounding boxes, `dedupe.COUNTRY_WORDS`, the SEO keyword filter's `COUNTRY_MASTER` words.
- **The CIS runtime role is `cis_app` (X1, AA-860; api + worker switch in S18).** Migration 244 grants it
  DML / sequences / non-trigger function EXECUTE in the CIS runtime schemas (11 then, 7 since CIS 299; not
  `catalog`, `booking`, `tripplanner`, `public`), default privileges for later objects (`FOR ROLE aa_cis_admin` in 244, restated
  `FOR ROLE cis_owner` by 245: the approved exception XD3, data standards Part A §9), read-only
  `shared.schema_versions`, and a permissive `<table>_cis_app_all FOR ALL TO cis_app USING (true) WITH CHECK
  (true)` policy on every RLS table, ENABLE-only **and** FORCE (the master reads FORCE tables unfiltered
  today; most code sets no GUC). **Every new RLS table in those schemas needs that policy in the same
  migration** (CI standing check `tests/integration/test_x1_cis_app_privileges.py`), and a guard asserting an
  exact policy set on an RLS table must allow it. This holds for an ENABLE-without-FORCE table too: `cis_app` is
  not the owner, so without a policy `TO cis_app` it reads 0 rows and its writes are refused, with no error. The
  isolation probe checks it nightly (cells `rls.enable_only_has_cis_app_policy`, `rls.force_parity.cis_app`). The
  conftest replay skips 244; the X1 tests apply and undo it. Live since X1 S18 (19/10/2026): api + worker
  `DATABASE_URL` = `aa-cis/dev/rds-cis-app`, and ECS-exec scripts run as `cis_app` too (the task role is denied the
  master secret since S19; master work goes through Infra DB Migrate `run-script`).
- **CIS schemas and objects belong to the NOLOGIN role `cis_owner` from migration 245 (X1 S21, AA-860).** 245 moves
  the 12 CIS schemas and every object the RDS master owned in them (the `catalog.*` views stay `catalog_owner`'s;
  `public.*` and the extensions stay the master's, they belong to the Infra `roles/` dir). From then on DB Migrate runs
  every `cis/` file as `cis_owner` (it switches to the live owner of schema `shared`; from X1 S22 the login is
  `db_migrator`), and default privileges `FOR ROLE cis_owner` give `cis_app` what a file creates. Rules for every
  later CIS migration: never `RESET ROLE` (go back with `SET LOCAL ROLE cis_owner`), no `SECURITY DEFINER`, no
  `CREATE/ALTER/DROP ROLE|EXTENSION` (→ Infra `roles/`). The legacy enums in `public` (`pipeline_status_enum`,
  `content_status_enum`, `review_status_enum`, … used by `raw_tours`, `generated_content` and others) stay the
  master's, so `ALTER TYPE public.<enum> ADD VALUE` (as in 073 / 158) fails as `cis_owner` ("must be owner of type"):
  it needs an Infra `roles/` file (runs as the master). `cis_owner` is **filtered by FORCE RLS** (unlike the master,
  it is not in `rds_superuser`), and every view without `security_invoker` reads its base tables as its owner, so 245
  gives it a permissive `<table>_cis_owner_all FOR ALL TO cis_owner USING (true) WITH CHECK (true)` policy on the 8
  FORCE tables: a migration that FORCEs RLS on another table adds both `<t>_cis_app_all` and `<t>_cis_owner_all` in
  the same file (CI standing checks in `tests/integration/test_x1_cis_app_privileges.py`), and a guard asserting an
  exact policy set must allow both. Only the master and `db_migrator` may be members of `cis_owner` (a member reads
  every tenant of those tables). The conftest replay skips 245; `test_aa860_migration_245.py` applies it as an
  RDS-like master after 244 and undoes it. Rollback is a prepared Infra `roles/` undo (a `cis/` file runs as
  `cis_owner` and cannot give objects back). The CIS migration freeze (Infra copy of 245 until X1 S22's 246 probe) is
  **over**: 245 applied 19/10/2026 12:11 UTC, 246 applied as `cis_owner` with login `db_migrator`, probe 58/58.
- **DB Migrate logs in as `db_migrator` from X1 S22 (AA-860)**, not the RDS master: every `cis/` file runs as
  `cis_owner` through the live-owner switch, and migration **246** (`246_x1_set_role_probe.sql`, assert-only) is the
  permanent proof (session user `db_migrator`, current user `cis_owner`, `db_migrator` a member of `catalog_owner`).
  The conftest replay skips 246; `test_aa860_migration_246.py` applies it as `db_migrator` under the switch, after
  245. The master keeps only Infra `roles/` files (roles, extensions, `public.*` enums), DB Migrate `set-password` and
  `run-script`; anything a `cis/` file cannot do as `cis_owner` goes there, never into a `cis/` file: no
  `CREATE/ALTER/DROP ROLE|EXTENSION` and no role attribute (`CONNECTION LIMIT`, `PASSWORD`) outside Infra `roles/`.
- **Verify the frontend with a full `next build`**: `tsc --noEmit` and eslint miss some type
  errors that break the Vercel build. The CI Lint job only runs flake8 on Python
  (`--max-line-length=120`).
- **Health and version (AA-884)**: `GET /health` returns `git_sha` (the image's `APP_GIT_SHA`, the merge commit) and
  `GET /health/ready` (public, 5 s in-process cache) returns 200 only when the DB answers and the newest numeric CIS
  migration in `shared.schema_versions` is ≥ `api/core/readiness.py` `REQUIRED_CIS_SCHEMA` (bump it in the PR whose
  code needs a newer migration). A needed number applied after a higher one (a reserved number, e.g. 297 after 299)
  goes into `OUT_OF_ORDER_CIS`: while it is not registered the reported version is capped just below it. Since AA-993
  M4a it also requires every key of `REQUIRED_CIS_KEYS` (each number from `LEGACY_CIS_BASELINE` + 1 = 264 up to
  `REQUIRED_CIS_SCHEMA`; a gap is `schema_behind` with `missing_schema_versions`) and lists `registered_keys` (every
  numeric key above the baseline, for the coming Deploy Dev `schema-gate`; omitted if that read fails). Deploy Dev's
  smoke asserts both, and that a live worker reports the same SHA (`shared.job_worker.git_sha`, written at worker
  start).
- **Release version (AA-891)**: release-please (manifest mode, `release-please-config.json`,
  `.release-please-manifest.json`, workflow `release-please.yml` with the CI bot GitHub App token) keeps one release PR
  open on `main`; merging it bumps the manifest, `CHANGELOG.md` and `api/version.py` (the only copy in code: FastAPI app
  version and `/health` `version`) and tags `vX.Y.Z`. Never edit the version by hand. A release-only merge does not
  deploy (`!api/version.py` in Deploy Dev's paths); the next code merge ships it, and the smoke asserts `/health`
  `version` equals the deployed commit's manifest.
- **API reference and breaking-change check (AA-899)**: `docs/api/openapi.json` is the OpenAPI document of `api.main`,
  exported by `scripts/export_openapi.py` (no DB, Redis, secret or AWS call; deterministic, no `servers`,
  `info.version` = `unreleased`). Unit Tests fails when it is stale (`tests/unit/test_aa899_openapi_export.py`, which
  also keeps the list of public routes without a response model, `tests/unit/snapshots/aa899_untyped_public_routes.json`,
  from growing). The "API contract" workflow (`api-contract.yml`) diffs it against the base branch with oasdiff
  (pinned) and fails a PR that breaks a public path (everything but `/admin/*`) unless the PR carries the label
  `api-breaking` or a `type!:` title; `/admin/*` breaks are warnings. FastAPI and Pydantic are pinned in
  `requirements.txt` for that reason. Each release gets the document as an asset (`aa-cis-openapi-vX.Y.Z.json`).
  How-to: `docs/api/README.md`.
- **Logs are JSON with request / job ids (AA-871)**: `shared/log_config.py` (called once by `api.main` and
  `python -m worker`, not under unit tests) renders structlog, stdlib and uvicorn lines as one JSON object each on
  `/ecs/aa-cis-dev` (`service` = `cis-api` | `cis-worker`, `git_sha`, `environment`). The outermost API middleware
  `api/middleware/request_id.py` accepts a well-formed `X-Request-ID` (else generates one), returns it on every
  response, binds `request_id` for the request and writes one `request_completed` line (method, path, route, status,
  duration; never query string, headers or body) in place of uvicorn's access log. `/v1/*` lines also carry the
  verified `tenant_id` (rate limiter); worker lines carry `job_id` / `job_kind`. Find a request with Logs Insights:
  `fields @timestamp, event, level | filter request_id = "<id>" | sort @timestamp`. Keep event names stable: Infra
  metric filters match them as substrings.
  An explicit `request_id=` on a log call (the T8/T9 angle-gate / content-writing request) is logged as
  `acp_request_id`, so the HTTP `request_id` stays on the line; use `acp_request_id` for the domain id.
- **Definition of "done" for a backend issue** (program rule): merge + green CI is not enough.
  Also verify (a) Dev deploy **rollout COMPLETED** (ECS running the new taskDef, old deployment
  drained) and (b) the **live endpoint** (curl → HTTP 200 + correct shape). Only then is it Done.

## Contracts with other apps

CIS is the content system of record (ADR 0001 / 0002). Other apps read what it publishes; they never
write CIS schemas. Owners per schema: `AA-Ecosys/docs/database/schema-ownership.md`.

| Consumer | Reads from CIS | Writes into shared | How it is granted |
|---|---|---|---|
| AA-TripPlanner-Web | the Model Gateway config `shared.llm_role_config` / `shared.llm_model_catalog`; all content through the `catalog.*` views (row below) | `shared.llm_call_log` (rows tagged `quality_signal.app = "tripplanner"`) | CIS migration 173 (gateway tables + log) to role `tripplanner`. The pilot's direct reads are revoked: 254 (AA-847) `raw_tours` (179) and `tour_atoms` (180); 270 (AA-712 X4) `gold_aa_internal.published_tours`, `shared.destinations` (201), the photo views `shared.v_destination_photos` / `v_tour_photos` (201), `acp_contract.v_active_tour_atoms` (216) and USAGE on `gold_aa_internal` / `acp_contract` |
| AA-TripPlanner-Web | Model Gateway config `shared.llm_role_config`, `shared.llm_model_catalog` (stages `tp_*`) | — | CIS 173 |
| AA-Booking, AA-TripPlanner-Web | **Catalog read model**, schema `catalog` (AA-679): `v_tour` (live AA tours, key `(source_system, source_id)` = `('cis', raw_tours.source_group_id)`, `content_hash`), `v_tour_day` (parsed canonical itinerary), `v_tour_place` (active-atom places + place card + destination lat/lng), `v_tour_photo`, `v_destination`, `v_destination_photo`. **Coordinates carry an attribution duty**: wherever a consumer shows `lat`/`lng` from these views (map pins, area circles), it must show "Powered by Geoapify" (link) + "© OpenStreetMap contributors" (AA-712, Geoapify Free-plan + ODbL terms); the place views of migration 228 expose `located_by` / `geo_precision` so a consumer draws `area` points as areas, not pins. Contract `AA-Ecosys/docs/contracts/catalog.tour.v1.json`; `catalog.tour.v1.3` (CIS 274, AA-986 L4) adds `v_tour_retired` (tombstone: retired / paused / restored per key, derived from the served state) and `v_tour_lifecycle_outbox` (served-state events, cursor `event_id`); CIS 298 (AA-996 follow-up) adds `v_tour_place_name` (security_barrier, SELECT to `booking` only): the `source_system`, `source_id`, `tour_id`, `day_number`, `position`, `place_name` of `v_tour_place` row for row, without its per-place card / destination / segment lookups, for the Booking trip-plans list; read keyed by `tour_id` only. New consumers read only `catalog.*`; since CIS 270 TripPlanner does too (plus the Model Gateway). Events `catalog.tour.published/updated/trashed` (outbox) are the next AA-679 slice | its own `booking.*` | CIS 225 to roles `aa_app_user`, `tripplanner`, `booking` (unguarded). Views owned by NOLOGIN role `catalog_owner` (no BYPASSRLS, no members), which reads `published_tours` / `raw_tours` through RLS policy `catalog_owner_aa_master_read` (AA master tenant only); consumers need not set `app.tenant_id`. 225 also makes `tenant_isolation` on those two tables treat `app.tenant_id = ''` as no tenant (NULLIF) instead of raising |
| AA-Partner (partner CRM) | **`catalog.http.v1` over HTTP** (AA-976, CRM-B4; root ADR 0010 decision 4: the CRM holds no grant on any CIS schema): `GET /catalog/v1/tours` (keyset cursor by `source_id`, `updated_since`, `country_code`, `limit` ≤ 50), `GET /catalog/v1/tours/cis/{source_id}` (days with per-day and per-place IANA `time_zone`, inclusions, exclusions classified for pay-on-arrival R25, facets for system tags R29, photos), `GET /catalog/v1/destinations/{id}`. Served from the `catalog` views read as `aa_app_user` on the tenant pool (one connection per api task at most, READ ONLY, `app.tenant_id` = ''), so it shows exactly what Booking / TripPlanner see. Liveness per root ADR 0015 decision 5: `retired` → 410 `gone` + `reason` (the CRM marks lines "no longer offered"), `paused` → 404 + `reason`, never served → 404. Strong `ETag` (a tour's `content_hash` = sha256 over the served body), `Cache-Control: private, max-age=60`, 304 on `If-None-Match`. Code: `api/routers/catalog_http.py`, `services/catalog_http/`; contract copy `docs/contracts/catalog.http.v1.openapi.yaml` (byte-identical to the root file, conformance test `tests/unit/test_aa976_catalog_http_contract.py`). **AA-authored content** (AA-977, contract 1.2): `GET /catalog/v1/aa-content` (latest published version of each item that is not retired; `kind`, `locale`, keyset cursor by `content_id`, `limit` ≤ 50; ETag / 304) and `GET /catalog/v1/aa-content/{content_id}/versions/{version}` (any published version, also after a newer publish or retire; a draft / unknown version → 404), read from `gold_aa_internal.v_aa_content_published` on the main pool (cis_app); see "AA-authored content" below | — | M2M client-credentials token of the `aa-m2m-<env>` pool, CRM client + scope `cis.catalog/read` (root ADR 0006b decision 7, `api/core/m2m_auth.py`, same shared verifier as staff tokens); API Gateway route `/catalog/{proxy+}` (AA-CIS-Infra) |
| AA-Partner (partner CRM) | **`tenant.membership.v1` over HTTP** (AA-978, CRM-P1; root ADR 0006b decision 4, ADR 0010 decision 5): `GET /tenant/membership/v1/tenants/{id}/members` (invited + active members and `tenant_state` active / locked / deleted; ETag over the body without `as_of`, 304), `GET /tenant/membership/v1/memberships?iss=&sub=` (keyed by iss AND sub), `GET /tenant/membership/v1/events?after=&limit=` (cursor feed, `limit` ≤ 500, ordered by `(txid, seq)` below the snapshot horizon so a late commit is never skipped). Ids, identity and the Content studio role only: no e-mail, name or CRM role. Read from `shared.tenant_members` / `shared.tenant_membership_event` (CIS 276) on the main pool (cis_app); see "Tenant membership directory" below. Code: `api/routers/tenant_membership.py`, `services/tenant_membership/`; contract copy `docs/contracts/tenant.membership.v1.openapi.yaml` (test `tests/unit/test_aa978_tenant_membership_contract.py`) | — | M2M token of the `aa-m2m-<env>` pool, CRM client + scope `cis.tenant/read` (`api/core/m2m_auth.require_m2m`, the same dependency factory as `/catalog/v1`); API Gateway route `/tenant/membership/{membership_proxy+}` (AA-CIS-Infra, AA-978 S1) |
| AA-Booking | **Supply content read-back** (AA-835, root ADR 0004a): `catalog.v_supply_content` (CIS 278), one row per Booking hotel / service CIS knows, key `(source_system 'booking', source_id = the Booking id)`: `content_status`, `content_version`, and `description`, `highlights`, `good_to_know`, `photo_urls` **only while published** (an approved version of an active entity; draft, in_review, stale and unpublished rows carry NULL text), `edit_url` = `/admin/supply-content/<id>` (Booking prefixes the CIS admin origin). Booking's hotel / service pages show it read-only and proposals carry only published text (AA-Booking `app/supply/content.py`, feature-detected) | — | CIS 278 to role `booking` only, under the `catalog.*:booking` contract grant (catalog_owner view, security_barrier) |
| Ops-probe Lambda (AA-CIS-Infra) | `POST /admin/dfs-balance/check`, `/admin/jev-canary/check`, `/admin/jev/402-sweep`, `/admin/waves/check` daily | — | admin secret |

CIS as a consumer (AA-858): the `/admin/overview` business view reads `booking.v_ops_funnel_daily`,
`booking.v_ops_case_status` (Booking bk-017) and `tripplanner.v_ops_traffic_daily` (TripPlanner tp-015) — aggregate
views the producers own and grant to `cis_app` in their own migrations (contract `ops.overview.v1`, AA-Ecosys
`docs/contracts/ops.overview.v1.md`; AA-CIS-Infra `db/owners.yaml` consumer_grants). CIS never reads `booking.*` /
`tripplanner.*` tables. The `/admin/data-coverage` page (AA-992) reads the family `ops.data_coverage.v1` the same way:
`tripplanner.v_ops_data_totals`, `v_ops_pin_components`, `v_ops_component_id_map` (tp-020) and
`booking.v_ops_data_totals`, `v_ops_trip_plan_keys` (bk-025), plus the CRM's counts over HTTP (`crm.ops.v1` 1.4
`GET /crm/ops/v1/data-coverage`, staff token relayed; no AA role reads schema `crm`).

CIS as a consumer of Booking supply facts (AA-835, root ADR 0004a; contracts `supply.hotel.v1` / `supply.service.v1`,
Booking bk-023 grants the four `booking.v_supply_*` views to `cis_app`). `services/supply_content/`:
- `pull.py`, one batch per api ops-sampler tick (`api/core/ops_metrics_sampler.py`, off with `SUPPLY_FEED_PULL=off`):
  `booking.v_supply_outbox` after the cursor in `shared.supply_feed_cursor` (row locked, SKIP LOCKED), the current
  facts from `v_supply_hotels` / `v_supply_services`, the inbox `shared.supply_event_inbox` (key = Booking
  `event_id`: a duplicate changes nothing; `applied` only when the facts' `supply_version` is newer, else
  `skipped_older`; `missing` when the entity left the view). An applied version stores the facts in
  `shared.supply_entity`, syncs `shared.supply_media` and queues one `supply_content_draft` job
  (`supply_content_draft:<source_id>:<supply_version>`); published content turns `stale` (no text served); a
  deactivated or missing entity turns `unpublished` (rows kept). A CIS photo copy whose media lost its usage rights
  or left Booking gets one `supply_media_purge` job (`services/jobs/supply_media_purge_job.py`: deletes both WebP
  objects, clears the copy columns, invalidates `/supply/<type>/<media_id>-*` on the photos CDN when
  `PHOTO_CDN_DISTRIBUTION_ID` is set; needs the task role's `s3:DeleteObject` on `photos/supply/*`). Lag, the last
  error and the purge jobs: `GET /admin/supply-content/status`.
- `supply_content_draft` job (`services/jobs/supply_content_draft_job.py`, background, concurrency 1, 2 attempts):
  kill switch `SUPPLY_CONTENT_LLM=off`, at most `SUPPLY_CONTENT_MAX_DRAFTS_PER_DAY` (20) paid writer calls a UTC day plus
  the cost guard row `bedrock / job:supply_content_draft`; copies rights-cleared photos (`photos.py`: source bucket /
  key from `booking.v_supply_media`, checked against the bk-023 naming rule and the sha256; WebP 1600 / 600 under
  `photos/supply/` + the img. CDN); one call on stage `supply_content_writer` (`writer.py`: facts quoted as untrusted
  data, source fidelity, logged to `shared.llm_call_log` with the job id; route Claude Sonnet 5.5 `sonnet-5-5` →
  legacy `sonnet` since CIS 282, S251: the 278 seed was the legacy key, which lost the schema); writes a new `llm`
  draft version. Never publishes.
- Review (`review.py`, `/admin/supply-content`): an edit is a new `human` version; publish (review.act) refuses an
  inactive entity, a version written against older facts (409 `outdated`) and a photo without usage rights or copy
  (409 `photo_rights`); reject, unpublish, regenerate (content.write). Version content is immutable in the DB (CIS
  278 guard trigger). Admin UI (CIS Content › Assets › Supply content): `/admin/supply-content` (list with the
  feed board, server-side filters and paging) and `/admin/supply-content/<id>` (the page `edit_url` names: status,
  open draft with its diff and publish blockers, published version, photo rights state, Booking facts, version
  history; Publish / Reject / Unpublish for review.act, Edit / Regenerate for content.write). Every staff role reads.

Platform services other apps may reuse (already used by TripPlanner where noted): the Model Gateway
and `llm_call_log` (TripPlanner), the durable job runner `shared.job` (CIS only for now — a second
enqueuing repo is AA-751), the cost guard `shared.spend_budget`, the Jev decision layer, and rerun
waves (`/admin/waves`, AA-727).

LLM operations (AA-900): `docs/llm/prompt-register.md` maps every gateway prompt to its stage, file and symbol, version
rule and eval baseline (guard `tests/unit/test_aa900_prompt_register.py`); `docs/llm/eval-runbook.md` is the by-hand
eval of a prompt, model or route change: an R2 run on the AA-729 golden set (`services/writing_memory/r2.py --stage
adhoc_aa900`, label `adhoc_aa900`, per-run cap <= 10 USD, the 25 USD writing-memory month cap) whose run ID and cost go
in the PR (template section "LLM change"). The route and model table is generated in the root repo
(`docs/generated/llm-routes.md`).

### AA-authored content (AA-977, CRM-B5)

AA staff write "Before you go" prep packs (R38), starter templates (R9) and campaign content blocks (R14) in CIS;
the partner CRM reads them over `catalog.http.v1` (row above) once published. CIS 275:
`gold_aa_internal.aa_content_item` (one locale of one piece; the EN and VI copies share `group_key`, same kind) and
`aa_content_version` (at most one draft, always the newest; published versions never change and are never deleted:
trigger `aa_content_version_guard`, rule E), view `v_aa_content_published` (published versions only: rule R). Grants:
`cis_app` only (no RLS: no tenant column, no personal data). Editor API `/admin/aa-content`
(`api/routers/admin_aa_content.py`, `services/aa_content/store.py`): drafts need `content.write`, publish / retire /
restore `master.admin`; every write carries the item's `updated_at` as `expected_updated_at` (409 `stale` otherwise).
At publish a prep pack whose visa section has no https link is refused (422 `visa_needs_official_link`: visa = an
official link only). Retire hides an item from the served list; its published versions stay fetchable by version.
`content_hash` = sha256 over the served AaContent body, computed once at publish (the ETag).
Editor page `/admin/aa-content` (CIS Content › Assets › AA content (CRM); every staff role reads, controls by
`can()`): a server-paged list, and a drawer per `group_key` with the EN and VI copies side by side (draft form, plain
preview, visa guidance mirrored from `validate.py` in `frontend/lib/aaContent.ts`, version history).

### Tenant membership directory (AA-978, CRM-P1)

Who belongs to which agency, with the CIS Content studio role (owner / editor / viewer; never a CRM role). CIS 276:
`shared.tenant_members` (identity `(cognito_iss, cognito_sub)` tagged `pii:online_id`, NULL while invited and bound
once at activation; status invited -> active -> removed, removed is final; one live row per identity and agency) and
the append-only `shared.tenant_membership_event`, written ONLY by triggers: on `shared.tenant_members` (invited /
activated / role_changed / removed) and on `shared.tenants` (created, and locked / unlocked / deleted when
`is_active` / `cancelled_at` change the agency state), so every admin tenant write path feeds it with no code
change. Deletes only 90 days after removal / occurrence or for an offboarded tenant (DM-7, AA-839 erasure). Grants:
`cis_app` only, no RLS, nothing for `aa_app_user`; the triggers run as the writer (not SECURITY DEFINER, MIG-6), so
a new writer of `shared.tenants` needs INSERT on the event table (migration guard + standing test). Staff routes
(Tenant 360 Members, `api/routers/admin_tenant360.py`, `services/tenant_membership/writer.py`): `GET
/admin/tenants/{id}/members` (identity shown as a 12-hex fingerprint only), `PATCH .../members/{membership_id}`
(Content studio role) and `POST .../members/{membership_id}/remove`, both `tenant.write`, audited
(`tenant.member_role_changed` / `tenant.member_removed`). The partner CRM applies a removal (member disabled and
signed out; last owner -> agency locked until owner recovery) and agency lifecycle within a minute; it never takes a
CRM role from CIS. Invitation and activation of identities come with AA-979 (partner-pool re-enrolment); until then
rows are created only by staff tooling / the Dev seed.
Tenant read (AA-978 follow-up): `GET /v1/account/members` (`api/routers/v1_account_members.py`,
`services/tenant_membership/portal.py`), the portal's Account › Members page: the agency's invited + active rows
(fingerprint, role, status, dates; no staff actor, no invitation id), tenant id from the tenant JWT only, main pool.

**Partner-pool Content studio sign-in (AA-979 P2b, root ADR 0010a).** The portal (AA-Partner `portal/`) swaps the
person's partner-pool access token (partner-web client) for a short tenant JWT at `POST /auth/tenant-session`
(`api/routers/tenant_session.py`; verifier `api/core/partner_identity.py`, its own `MultiIssuerVerifier`, never a staff
issuer; WORKFORCE / EXTERNAL / M2M tokens 403). It answers from the active `shared.tenant_members` rows of `(iss, sub)`
on active agencies (`services/tenant_membership/session.py`): none 403 `no_membership`, several 409 `choose_tenant`
(chooser `GET /auth/tenant-memberships`). The token is the API-key token's shape plus `client=portal`, `amr=partner`,
`mid`, `csr`; at most 15 min and never past the access token. Throttled per identity in Redis, not per IP. The Content
studio role is enforced by `api/middleware/tenant_role.py` on every tenant-JWT path (`/v1/*`, `/admin/brand-identity`):
viewer read only, editor no writes under `/v1/integrations`, `/v1/account`, `/v1/webhooks`; owner and API-key tokens
unchanged. Invitations: `POST /admin/tenants/{id}/members/invitations` (`tenant.write`) creates the `invited` row and
relays the e-mail once to the CRM (`crm.ops.v1` `POST /tenants/{id}/studio-invitations`, staff token, through
`api/services/crm_console.py`); CIS stores no e-mail; `.../members/{membership_id}/invitation/send` sends again. The
CRM binds the accepted identity with `POST /tenant/membership/v1/invitations/{invitation_id}/activate` (M2M scope
`cis.tenant/activate`, contract 1.1; idempotent for the same identity, 409 otherwise). API-key sign-in
(`/auth/tenant-login`) is unchanged for machines; the portal stopped offering it at the P2b clean-up
(`PORTAL_API_KEY_SIGNIN=false`: its `/tenant-login` answers 308 to the partner sign-in).

## Staff roles and permissions (AA-656)

One staff identity model. Every staff user is a row in `shared.admin_users` (role enum
`shared.admin_role`: `admin`, `reviewer`, `content` — migration 218 — and the read-only `viewer` — migration
249, AA-878; identity columns from migration 247:
`email` lower-case `@adventure.asia` only — one TEMPORARY exception until the Cognito E2E work is done (AA-816,
S243 option B, root ADR 0006a): the exact address `e2e@e2e-mail.lumiguides.it.com` as a reviewer, not an owner,
no password, no Booking grant (migration 250 CHECK + `staff_directory.E2E_TEST_STAFF_EMAIL` /
`require_e2e_test_scope` + the WORKFORCE triggers); and, **Dev only** (A10b, AA-816 S252, root ADR 0006a A10b),
`e2e-cis-admin@e2e-mail.lumiguides.it.com` as an admin, not an owner, no password, no Booking grant (migration 283
CHECK + `api/core/e2e_staff.py` / `staff_directory.require_e2e_test_scope` / `require_dev_only_e2e_environment` +
the WORKFORCE triggers' dev-only map) —, `display_name`, `cognito_iss` + `cognito_sub`, `last_login_at`
("last seen"), `is_owner`; `password_hash` nullable, NULL fails closed). A staff member's role in another app
lives in `shared.staff_app_grant (admin_user_id, app, role)` (`app` = `booking`, roles `admin` / `sales` /
`contracting` / `content_creator`). Root ADR 0006 + amendment 0006a are the design; tenants are separate
(`cis_tenant_token`, role `tenant`).

**Dev-only E2E CIS admin (A10b, AA-816 S252, root ADR 0006a A10b).** The agent's own admin identity for evidence
screenshots of the admin-only pages and the AA-916 WM-07 sentence-action sample. `api/core/e2e_staff.py` holds the
rules: the directory creates, re-enables or syncs it only where `ENVIRONMENT` is exactly `dev` (403
`e2e_not_allowed_here`; disable stays allowed everywhere); `staff_identity.resolve_staff` refuses its token (403
`e2e_not_allowed_here`) unless `ENVIRONMENT` is exactly `dev`, else flags the identity `e2e_read_only` (log
`staff_identity_e2e_read_only`, owner flag forced false); the bearer middleware answers every non-GET/HEAD/OPTIONS on a
staff path with 403 `e2e_read_only` before the route runs, except POST
`/admin/content-quality/{pieces,tour-versions}/{id}/sentences/apply` (Cut / manual Edit with a reason chip; the
routes accept only `cut` / `manual` from it, `…/suggest` stays refused) and, since AA-979 (root ADR 0006a A10b-1,
S252), POST `/admin/tenants/{id}/members/invitations` and `/admin/tenants/{id}/members/{membership_id}/invitation/send`
(the phase 2 re-enrolment E2E); those two routes then refuse it (403 `e2e_read_only`, before any row, audit or relay)
unless the tenant is synthetic (`shared.tenants.is_canary IS TRUE`), the address is on the E2E mailbox domain
(`e2e-mail.lumiguides.it.com`) and the invited role is `viewer` / `editor`; `staff_authz.denial_reason` refuses the same
writes again by route template. `/auth/verify-admin` returns `admin`, so the BFF opens the admin pages. Its sign-in
codes land in the mailbox prefix `inbound-admin/`, readable only by the MFA admin role, so only the orchestrator signs
in with it, locally (Playwright project `admin-evidence`, `E2EADMIN=1`, never CI). Removed with A10 / A10a at the end
of project code.

**Sign-in (ADR 0006 Phase 1, live on Dev).** Staff sign in with an email one-time code on Cognito WORKFORCE
(pool `us-west-1_bCcFvbSOS`) through managed login on our own domain `https://staff-auth.lumiguides.it.com`
(AA-branded; the prefix `amazoncognito.com` domain tripped a Safe Browsing false positive). `/login` shows
"Sign in with company email" and never redirects by itself.
- The BFF (`frontend/lib/cognito.ts`, `lib/cognitoRoutes.ts`, routes `app/api/auth/{signin,callback}/{cognito,
  cognito-breakglass}`, `app/api/auth/refresh`) runs authorization code + PKCE (state + nonce in the 5-minute
  `cis_oauth_tx` cookie) with a confidential client. Redirect URIs come from a fixed allow-list
  (`aa-cis.lumiguides.it.com`, localhost in development; a Vercel preview build only the fixed Preview host,
  below); any other host (a per-deployment `*.vercel.app` preview URL) gets `cognito_host` and no sign-in at all (the
  password form is off since AA-816 S2). Domain, issuer and client ids are code constants with `COGNITO_*` env overrides; the two production client
  secrets are set in Vercel (Production only), the Preview-only client secret on the Preview target only.
- Cookies: `cis_admin_token` holds the Cognito **access** token (15 min; the cookie lives for the refresh window
  so `proxy.ts` can see it expired), `cis_staff_rt` the refresh token (httpOnly, `path=/api/auth`, 12 h,
  1 h for break-glass; the window is not extended by rotation). Refresh happens only in `/api/auth/refresh`
  (single-flight; `proxy.ts` redirects page navigations there, `SessionKeepAlive` POSTs before expiry).
  `cis_role` / `cis_user` are display hints set by the server, never trusted. Logout (`POST
  /api/auth/admin-logout`, same-origin only — a cross-site POST gets 403, like Booking's) revokes the refresh
  token, clears every session cookie and answers the managed-login `/logout` URL (`client_id` + `logout_uri` =
  `<prod>/login`, registered on both staff clients); the Topbar follows only that URL or `/login`
  (`lib/staffLogout.ts`), so the next sign-in asks for a new email code.
- A sign-in completes only after `/auth/verify-admin` accepts the token; a refusal lands on
  `/login?error=no_access` with no session cookies.

**Verification (backend).** `api/core/identity_verifier.py` (app-neutral, to be copied byte-identical into
AA-Booking) + `api/core/identity_config.py` (CIS surface) + `api/core/staff_identity.py` (resolver):
- issuer chosen by the **unverified `iss`**; only the WORKFORCE issuer is accepted for staff (any other `iss` →
  401, a JWT without `iss` → the legacy HS256 path); RS256 only, `client_id` ∈ {`cis-admin`, `breakglass`,
  `cis-admin-preview` (AA-911 D-1, always capped at viewer, below)},
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
  `/api/tenant/*`.
- **Principal-aware secret check (AA-816 C9, `api/core/staff_credential.py`).** On a staff path every admin-secret
  guard (`admin.verify_admin_secret`, `admin_settings.verify_admin`, `v1_pipeline._require_admin`,
  `core/staff_secret.py`, `staff_policy_if_staff`, the staff audit and the rate limiter's admin tracking) accepts a
  request whose Cognito Bearer the middleware verified, **without** `X-Admin-Secret`; POLICY still applies to the
  token's role. The BFF (`lib/auth-server.ts` `staffCredentialHeaders`) sends a Cognito session's Bearer **and no
  secret** on the staff prefixes (`STAFF_API_PREFIXES`, kept equal to `STAFF_PATH_PREFIXES` by a test); a legacy
  HS256 session, and any path outside them (`/api/tour-full` → `/v1/tours/{id}/full`), still sends the secret.
  **Preview credential:** a second secret read from Secrets Manager by the code constant
  `PREVIEW_SECRET_NAME` (no env var; only on the deployed API, `AWS_EXECUTION_ENV`; cached 5 min, a miss 1 min;
  missing, unreadable or equal to the main secret = feature off, never an error). Sent as `X-Admin-Secret` on a
  staff path, it makes the middleware replace `x-admin-role` with `viewer` (no role header → viewer, never the
  machine), so it reads content and is refused on every write and admin-only read; audited as
  `details.auth = preview`. The Vercel Preview BFF holds this value; the main `ADMIN_SECRET` stays with the
  Production BFF (legacy sessions), the CI deploy gate/smoke and the ops-probe Lambda.

**Legacy password login (off since AA-816 S2, kept as the rollback).** `/auth/admin-login` (HS256 JWT signed with
`JWT_SECRET`) still answers. `LEGACY_ADMIN_LOGIN_ENABLED` gates **only** the CIS UI password form + BFF
`/api/auth/login` (404 when off) and the HS256 path of `/auth/verify-admin` (401 "Password sessions are switched
off; sign in again" when off). **Since S2 it is off by default in both the API and the BFF**: only an explicit
`1` / `true` / `yes` / `on` (case and spaces ignored, same parsing in `api/core/staff_identity.py` and
`frontend/lib/staffSession.ts`, a test keeps the two lists equal) turns it back on; unset, empty or a typo keeps it
off. The deployed API task definition does not set it, so every HS256 staff session is refused on every host,
Preview included; staff sign in with Cognito only. Rollback: revert the S2 commit, or set the flag on the api task
definition (AA-CIS-Infra, hand edit) and on the Vercel target. `/auth/admin-login` and `/auth/booking-token` are not
gated and never call `/auth/verify-admin`: AA-Booking's legacy staff path (admin-login → booking-token) keeps working
until the bridge is retired (AA-911 P6b). The fixed Preview host `aa-cis-preview.lumiguides.it.com` (below) never
offers the password form (`/login` hides it, BFF `/api/auth/login` answers 404 there).

**Preview staff sign-in (AA-911 D-1, AA-816 P6a, root ADR 0006a A11).** `aa-cis-preview.lumiguides.it.com` is a
Vercel domain of `aa-cis-frontend` and the only callback / sign-out URL of the Preview-only WORKFORCE client
`cis-admin-preview` (AA-CIS-Infra; the production `cis-admin` client does not list it; break-glass stays
production-only). A Vercel production build signs in with `cis-admin` on the production host only, a preview build
with `cis-admin-preview` on this host only (`VERCEL_ENV`, `frontend/lib/cognito.ts`; its own secret env var
`COGNITO_CIS_ADMIN_PREVIEW_CLIENT_SECRET`, Preview target only; client id a code constant), so the redirect URI is
still never built from the Host header and a preview build never reads the production client secret. The Vercel
Deploy workflow points the host at a preview only on opt-in: the PR label `preview-signin`, or a dispatch from
`main` with `alias_only_url` (checked over the Vercel API: this project, not production, READY). A Cognito session
on that host is always the **viewer**: `api/core/staff_identity.resolve_staff` maps every `cis-admin-preview` token
to role `viewer` with `is_owner` false whatever its `cis-*` groups (`api/core/identity_config.is_preview_client`,
env override `COGNITO_CIS_ADMIN_PREVIEW_CLIENT_ID`; a preview id equal to a production id fails the verifier build,
so every staff token answers 503), the bearer middleware forwards `viewer`, `staff_authz` refuses every write
("Role 'viewer' is read-only") and `/auth/verify-admin` returns `viewer` (log `staff_identity_preview_capped`). The CIS
to Booking bridge (`/auth/booking-token`, below) stays and mints only from an HS256 password session, so a Cognito
session (this one included) never gets a Booking token; the bridge retirement is AA-911 P6b. The
cap holds only while the Vercel Preview `ADMIN_SECRET` is the C9 Preview credential (check V2, root ship plan):
the main secret with a forged `x-admin-role` would pass on a path without a Bearer. Preview builds send
`X-Robots-Tag: noindex, nofollow`. Every BFF route that attaches `X-Admin-Secret` calls `requireAdmin()` first
(audited 21/10/2026). UI smoke keeps the per-deployment URL. Runbook: root
`docs/ops/runbooks/preview-staff-sign-in.md`. For an HS256 session (rollback only, since S2)
`/auth/verify-admin` re-reads `shared.admin_users` (cached 60 s), so a deactivation or demotion lands within
~6 min (BFF verify cache 5 min + 60 s). Cutover preconditions (ADR 0006): every active row has an email and a
`cognito_sub` from that person's own sign-in, managed-login break-glass tested; the secret check is
principal-aware since C9 (above); then `ADMIN_SECRET` is rotated (I6) and the flag goes off. New staff are created on
the Staff page only (the legacy password-row script is retired, AA-816 D1).

**Login throttle (AA-876).** `/auth/admin-login` and `/auth/tenant-login` call `api/core/login_throttle.py` before the
DB read and bcrypt (bcrypt runs in the threadpool). One pipelined Redis call checks, per source IP and scope: at most
20 **failed** logins per 15 min (`loginfail_ip:*`, successes do not count) and a high ceiling of 100 attempts per 5 min
(`loginrl:*`, protects bcrypt CPU). Admin only, per username (stored as a hash, never in clear): 5 failures from one
IP start an exponential wait for that (username, IP) (30 s doubling, max 15 min; a success clears it), and 50
failures across IPs in 15 min start a 5 min wait for the username everywhere (distributed guessing). Backoff only,
no hard lockout: every key expires by itself. Every 429 has the same body + `Retry-After` and logs
`login_throttled` (scope, reason, IP). Redis missing / failing → fail open (`login_throttle_redis_error`). The IP is
the **rightmost globally routable** `X-Forwarded-For` entry: API Gateway appends the real source and the NLB/ALB hops
are private, so a caller cannot choose its bucket. This depends on the edge being API GW → VPC link (NLB) → ALB; a
proxy in front of API GW or a direct ALB path breaks it. Limitation: through the BFFs (admin, portal, Booking bridge)
the "IP" is a shared Vercel egress address, so an attacker failing through a public BFF can exhaust that bucket for
everyone behind it for up to 15 min (follow-up: a signed forwarded client IP from the BFF, or retiring legacy login).
The BFFs pass a 429 on as a 429 with a readable message (not 502). Cognito sign-in is not affected. The API Gateway
stage also throttles the two login routes (Terraform, AA-CIS-Infra).

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

**Data model (AA-894 scope 2).** Developer › Data model `/admin/data-model` (admin only: PAGE_RULES, BFF `API_READ_RULES` "data-model", POLICY `GET /admin/data-model` = `ops.read`; next to API Docs, outside `/admin/platform/*` like it). Router `api/routers/admin_data_model.py`, service `api/services/data_model.py`: reads the ERD model `erd.json` (format 1) that the AA-CIS-Infra nightly DB Schema Dump writes to the bronze bucket at `data-model/erd/latest.json` (+ `previous.json`, the model before the last structural change; prefix `DATA_MODEL_S3_PREFIX`), returns it with a diff summary (tables / columns / keys / FKs added, removed, changed), cached in process 5 min (`?refresh=1` at most every 30 s). 404 when no model is published, 503 when storage fails. No DB query. This repo is public: it ships the reader and the renderer (`frontend/app/admin/data-model/`), never a table list; the names exist only in the S3 object and the admin-only response.

**Docs reader (AA-898).** Developer › Docs `/admin/docs` (admin only: PAGE_RULES, BFF `API_READ_RULES` "docs", POLICY `GET /admin/docs/manifest`, `GET /admin/docs/page` and `GET /admin/docs/asset` = `ops.read`). A reader, never an editor: docs change by PR in git. The root repo workflow "Docs sync" (`.github/workflows/docs-sync.yml`, `tools/docs/build_docs_reader.py`) builds the bundle from a CI checkout of the docs catalog (publication rule v1: catalog pages and ADRs only; journals, audits, security pages and templates never; account and resource IDs redacted, a guard fails the build) and uploads it to the bronze bucket under `docs-reader/` (`manifest.json` + `pages/<repo>/<path>.md`; prefix `DOCS_READER_S3_PREFIX`); the CIS task role may only read that prefix (AA-CIS-Infra `modules/ecs` ReadDocsReader / DenyDocsReaderWrites). Router `api/routers/admin_docs.py`, service `api/services/docs_reader.py`: manifest cached in process 5 min (`?refresh=1` at most every 30 s), page bodies 5 min (64 kept); a page is read only by the `key` its manifest entry names, after a shape check, never by a caller path. Owner-only entries (`access: owner`; any other value counts as owner) are listed and served only to a principal holding `docs.owner_read` (`staff_authz.can_read_owner_docs`: `auth = cognito` and `is_owner` (N-3); TEMPORARY, Dev only, the A10b E2E CIS admin, `is_owner` still false); for anyone else they do not exist (404). Translations (root rule 8): entries carry `lang`, `translation_of`, `translations`; links are re-checked both ways, a copy of an owner page is owner, the list shows English pages and the reader has an EN / VI switch (remembered per browser). 404 when nothing is published, 503 when storage fails. Rendering: the CIS-local kit file `frontend/app/_kit/MarkdownView.tsx` (react-markdown + remark-gfm, raw HTML dropped, images not loaded except the bundle's SVGs below, links resolved by the page: published page → reader, https/mailto → new tab, anything else → plain text; GitHub callouts via `lib/markdownCallouts.ts`; ```mermaid blocks drawn by `app/_kit/MermaidDiagram.tsx`, lazy, strict, DOMPurify-sanitised, self-contained SVG only, source on error); helpers in `frontend/lib/docsReader.ts`. **SVG images (AA-697, root rule 9):** the manifest may list `assets` (SVGs
under `docs-reader/assets/<repo>/<path>.svg`, the architecture diagrams); `GET /admin/docs/asset?repo=&path=` (POLICY
`ops.read`) serves one only when the manifest lists it for the caller, re-checks it on every read
(`docs_reader.svg_is_safe`: no script, `foreignObject`, other active element, event handler, `javascript:`, CSS `url()` /
`@import` or entity; every href a fragment or an inline PNG; no 12-digit run outside the inline PNGs; else 503, never
served) and sends it as `image/svg+xml` with a sandbox CSP + nosniff, which the BFF re-adds (`lib/upstreamHeaders.ts`).
The manifest response lists `assets` as `{repo, path}` (no key). A page's `![alt](relative.svg)` that resolves to a listed
asset (`resolveImage` in `lib/docsReader.ts`) is drawn by `MarkdownView` as `<img>` in a white frame linking to the
full-size image; every other image stays alt text. This repo is public: no page content lives here.

**Platform apps, audit and cost (AA-884).** Router `api/routers/admin_platform_ops.py` (read only, admin-only POLICY
entries; no deploy/scale button, no AWS/Vercel/GitHub write token):
- `GET /admin/platform/apps[/{app_id}]` (`ops.read`) — `api/services/platform_apps.py` `collect_apps()`: one card per
  app from the backend registry `api/core/app_registry.py` (code constants). CIS: API SHA + task revision, live
  workers' SHA (parity), the deploy gate (`shared/jobs/worker.py` `deploy_gate_status`, same as the CI guard), schema
  readiness. Booking: HTTP `/booking/health` + `/booking/health/ready` (2 s). TripPlanner: no health endpoint yet;
  today's customer-facing LLM spend (TP `ONLINE_STAGES`, `quality_signal->>'app' = 'tripplanner'`) vs the mirrored
  default cap (5 USD; the live cap is a TP env var). Partner CRM (AA-936, AA-Partner): HTTP `/crm/health` +
  `/crm/health/ready` on the partner host, like Booking. Cached 60 s in Redis, built only on a page view (no poller).
  No ECS/Lambda describe calls, no cross-schema reads (CIS never reads schema `crm`).
- `GET /admin/platform/apps/crm/jobs` (`ops.read`, AA-936) — `platform_apps.crm_jobs()`: the CRM's `crm.job` per kind
  from the CRM contract `crm.ops.v1` `GET /crm/ops/v1/jobs/summary`, called with the signed-in staff member's own
  Cognito Bearer (the CRM's OPS route class accepts WORKFORCE `cis-admin` tokens only; a legacy session gets
  `cognito_session_required` and no call). Not cached (per person). The cost page has a `crm` row from the App tag.
  The token comes from `platform_apps.relay_authorization(request)`: StaffBearerMiddleware drops the Authorization
  header after verifying it and keeps the token in `scope["state"]["staff_access_token"]` (AA-941; never logged).
- `/admin/platform/crm/*` (AA-941, CRM-13a S-01; GET `tenant.read`, writes `tenant.write`) —
  `api/routers/admin_crm_console.py` + `api/services/crm_console.py`: the partner CRM's AA L1 console (agency list and
  meters, create agency, lock / unlock, first-owner invite, allowed features, insights opt-out, AA-raised data
  requests, owner recovery, member disable, failed-job retry). A relay only: each route mirrors one `crm.ops.v1` route
  with the staff member's own Cognito token, inside a path allow-list (`ALLOWED`); the CRM's status and body pass
  through (it decides crm-platform-admin writes and filters L1 keys); bodies are never logged (the owner invite
  carries an e-mail). Page `/admin/platform/agencies` (nav Platform › Tenants › Agencies (CRM)).
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
  `staff.request` / `machine.request` row per non-GET call on a staff path that carries a staff credential (the admin
  secret, a verified Cognito Bearer, or the Preview credential, AA-816 C9): actor
  from the verified Cognito identity, else `x-admin-user-id` marked `details.auth = legacy_unverified`, method,
  route template, status (denied calls included); never the body or query values. `GET /admin/platform/audit`
  (`audit.read`, filters + keyset cursor, staff scope by default) and `GET /admin/platform/audit/verify`
  (`audit.read`; Python recompute of the chain, `api/services/platform_audit.py`, must stay equal to the trigger;
  a row without `chain_seq` created after 248 was applied — a trigger bypass — also makes it `ok: false`).
  Since X1 the runtime `cis_app` cannot touch the triggers (S18, not the owner); the owner `cis_owner` (only through a
  reviewed DB Migrate file) and the break-glass RDS master still could, and the chain makes such an edit detectable,
  not impossible. Integration tests delete their own audit rows only through
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
  AI so it is not counted twice; acc3 Bedrock CE actuals are a cross-check line (`llm_satellites`; a `retired` entry
  only for a month with acc1 history, AA-998). `GET /admin/platform/cost`
  (`spend.read`) refreshes a month whose snapshot misses days or is > 24 h old (and may still change) with one CE
  request (~$0.01) behind a Redis lock, at most once a day per window (a Redis attempt marker, also after an empty
  or failed answer); `POST /admin/cost-explorer/check` (the "Refresh from AWS" button) also
  fetches the App tag (+1 request). No scheduler yet.
- **Alert recipients (AA-913).** Who gets the platform alert e-mails, chosen on the admin page instead of a
  Terraform variable. One list for everything: the CIS api manages the **e-mail subscriptions of the SNS alerts
  topic** (CloudWatch alarms of CIS, Booking, TripPlanner and shared; security EventBridge rules; cost anomalies;
  AWS Budgets, which notify the topic since the AA-913 Infra change), and `ops_alert_email` mails the confirmed
  rows. Table `shared.alert_recipient` (CIS 251: e-mail, note, `pending`/`confirmed`/`removed` +
  `removed_reason` `admin`/`unsubscribed`/`expired`, the subscription ARN — never returned to the browser —, who /
  when; rows are history, `cis_app` has no DELETE). Env from Terraform on api + worker: `ALERTS_TOPIC_ARN` and
  `ALERT_BOOTSTRAP_EMAIL` (the one Terraform-managed subscription, `var.alert_email`: shown **locked**, never stored
  or changed by the app, so alarms still reach someone if the app or the DB is down); either unset = page read-only
  "not configured", writes 503 (fail closed: no unsubscribe path can then tell the bootstrap apart). Every
  unsubscribe checks the SNS endpoint against the bootstrap, and the Infra grant denies `sns:Unsubscribe` on its ARN. Code: `shared/alert_recipients.py` (e-mail rule, SNS calls in a thread with 3 s/8 s timeouts,
  pure `plan_reconcile` + `reconcile`, `ops_recipients`), `api/services/alert_recipients.py` (page, add, remove,
  resend, test e-mail, rate limits from `acp_shared.audit_log`), router `api/routers/admin_alert_recipients.py`:
  `GET /admin/platform/alert-recipients[?refresh=true]` (`ops.read`; reconciles with SNS first, throttled 60 s in
  Redis), `POST …` add, `DELETE …/{recipient_id}`, `POST …/{recipient_id}/resend`, `POST …/test-email`,
  `POST …/refresh` (all `alerts.write`). Add / remove / resend / test also need a **Cognito session** (checked in the
  router; the topic carries security alerts), refresh does not. AWS sends and validates the confirmation e-mail
  (no token handled here). Reconcile: SNS confirmed -> `confirmed`; gone after confirmation -> `unsubscribed`;
  never confirmed (AWS drops it after 3 days) -> `expired`; an address removed by an admin whose confirmation was
  clicked later is unsubscribed again — the api's ops metrics sampler reconciles every 5 minutes, so that happens
  without a page view; an address removed while still pending is listed as "leaving" until AWS drops it; other topic
  subscriptions are listed read-only ("unmanaged"). A 429 carries `retry_after_s` in the body (the BFF forwards only
  Content-Type). Limits: 10
  active, 10 adds / 24 h, resend 1 per 10 min and 3 per 24 h per row, test e-mail 1 per 5 min (429 + Retry-After).
  IAM (AA-CIS-Infra): Subscribe (e-mail protocol), ListSubscriptionsByTopic, Unsubscribe, GetSubscriptionAttributes
  on that topic only; no `sns:Publish`, so the test e-mail tests the SES path and SNS delivery is checked with
  `aws cloudwatch set-alarm-state`. Page `/admin/platform/alerts` (People & access › **Alert recipients**). Notes:
  `docs/implementation-notes/AA-913.md`.
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
**Data coverage (AA-992).** `GET /admin/data-coverage` (`ops.read`, admin only, not a machine route) feeds
`/admin/data-coverage` (Platform › Overview): ≈ 140 rows in 9 stages (inputs, raw, S1 / Master, A-series, places,
T-series, TripPlanner, Booking, CRM), counts only. Package `api/services/data_coverage/`: `defs.py` (the live-tour,
active / place atom and served-destination fragments of the S252 audit), `queries.py` (one statement per source),
`registry.py` (sources + rows: kind must / backlog / info, polarity coverage / defect / threshold / equal, expected
gap, drill link), `sources.py` (each CIS statement in its own READ ONLY transaction, `statement_timeout` 4 s,
`lock_timeout` 1 s, `wait_for` 5 s, 3 at a time; the `catalog.*` views as `aa_app_user` through
`catalog_http.reader.catalog_connection`, one statement per connection, the four catalog sources one after another
(`Ctx.catalog_lock`); S3 `ListObjectsV2` of `raw-inbox/<AA>/` and
`brand-identity/`, ≤ 10 pages; the CRM over `crm_console`), `build.py` (tiers on `ops_overview.cache.cached_section`
under `admin:data-coverage:*`: fast 60 s, slow 600 s, CRM 600 s on its own key and only with a Cognito token; last
good 24 h; `?refresh=1` recomputes the fast tier only when older than 15 s). A failing source turns only its rows
`na` with the reason (a tier past its 12 s deadline keeps the sources that finished; only the pending ones are
`timeout: time_budget`); only a final answer is `unavailable` (`contract_view_missing` for an owner view not applied
yet, `cognito_session_required`, no tenant pool), a busy catalog gate or any CRM non-200 is `error` (cached 60 s,
never the last good copy). $0: no model call, no DFS.
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
| Overview, Photos, Social Content (atom curation), Content Trace, A4 Quality (`/admin/platform-stats`), Content Quality, SEO Intelligence, Tenants, Cost Operations, Jobs, Jev Decisions, API Docs | ✓ | — | — | — | — |
| Platform pages `/admin/platform/*` (AA-883 Staff directory; writes need `staff.write`, owners only; AA-913 Alert recipients, changes need `alerts.write` + Cognito) | ✓ | — | — | — | — |
| BFF `/api/admin/openapi.json` and `/api/admin/platform/*` (any method; `lib/rbac.ts` API_READ_RULES, backend re-checks the role) | ✓ | — | — | — | — |
| Backend docs `openapi.json`, `docs`, `redoc` through the staff branch of `/api/tenant/*` (`lib/rbac.ts` isApiDocsPath) | ✓ | — | — | — | — |
| Tenant portal `/portal/*` — **portal app** (AA-Partner `portal/`, `portal.lumiguides.it.com`, AA-885 / AA-979; no staff access) | — | — | — | — | ✓ |
| BFF proxies `/api/admin`, `/api/pipeline`, `/api/tour-full`, `/api/tenant/pipeline/brand-identity`, `/api/tenant/*` (admin app; `/api/tenant/*` is staff-only since AA-885) | ✓ | ✓ | ✓ | GET only | — |
| AA-Booking token `POST /auth/booking-token` | ✓ | ✓ | ✓ | 403 | — |
| `/api/tenant/*`, `/api/tenant/me`, `/api/playground/proxy` — **portal app** BFF, tenant JWT only | — | — | — | — | ✓ |

**Read-only `viewer` (AA-878).** The role of every Preview-host Cognito session (AA-911 D-1) and, until AA-816 S2,
of the UI-smoke E2E account (`e2e-claude-code-viewer`, a legacy
password row; password only in Secrets Manager `aa-cis/dev/e2e-test-viewer`; created in S242 with the legacy
row script, retired at AA-816 D1 — a password change for a legacy test row is now a reviewed DB Migrate
`run-script`). Capabilities `content.read` only. It is refused on
**every** non-GET twice: the BFF `requireAdmin()` answers 403 before attaching the secret (`lib/rbac.ts`
`READ_ONLY_ROLES`), and the backend `denial_reason` refuses `READ_ONLY_ROLES` on any method but GET/HEAD/OPTIONS
whatever the route's capability (the notification bell's PUTs need `content.read`, the viewer still cannot clear
it). Admin-only reads stay refused by POLICY; the role-filtered open reads (search, bell, tenant details) treat
it like reviewer/content. No Booking access (`/auth/booking-token` → 403, `staff_authz.NO_BOOKING_ROLES`). Not
assignable from the Staff page (`staff_directory.CIS_ROLES`, `lib/rbac.ts` `ASSIGNABLE_STAFF_ROLES`) and no
Cognito group (a `cis-viewer` group is deferred to AA-816 E1); the reconcile report lists the row under cutover
readiness (no email / sub), a legacy-only exception. UI smoke runs in **viewer mode** when the smoke session is
the viewer (since the AA-816 S2 path: the Cognito E2E user on the fixed Preview host, capped at viewer by the API;
before it, this legacy account): readable pages get the full page check, admin-only pages must redirect to
`/admin/review`, a BFF write and the staff list must answer 403. A smoke-public test keeps the page list's
`viewerRefused` flags equal to `PAGE_RULES`.

AA-Booking staff roles map onto the same three: `admin` → admin, `sales` → reviewer tier,
`content_creator` → content (the CIS `viewer` maps to nothing: no Booking token). Reviewer and content reach the backend through the same admin secret;
what they may call there is narrowed by the backend capabilities below (AA-882), not by the BFF yet
(`canWriteAdminPath` still allows writes by default until GĐ4b).

**Backend capabilities (AA-882, S241 GĐ1 + GĐ3).** `api/core/staff_authz.py` holds `ROLE_PERMS` (role →
capabilities: `content.read/write` and `review.act` for every staff role; `master.admin`,
`atoms.write`, `photos.write`, `a4.unpublish`, `quality.write`, `content.settings.write`, `tenant.*`,
`ops.*`, `ai.admin`, `spend.*`, `staff.read`, `audit.read`, `alerts.write` (AA-913; the alert-recipient changes
also need a Cognito session, checked in their router) for admin (AA-977: AA-authored content drafts need
`content.write`, its publish / retire / restore `master.admin`); `staff.write` is an owner flag,
not a role permission) and `POLICY` keyed by `(method, route template)`. The router-level dependency
`staff_policy` (secret check, then policy) refuses with 403 a role that lacks the route's capability;
`staff_policy_if_staff` does the same only for a request carrying a staff credential (the valid secret, or since
AA-816 C9 a verified Cognito Bearer or the Preview credential), on the routers
whose own guard also admits a tenant JWT or answers 401 (`admin_pipeline`, `admin_atoms`,
`/v1/pipeline`, `/v1/rules`, `/v1/social`). The role is the `x-admin-role` the BFF forwards
from the verified token; for a Cognito Bearer the `staff_bearer` middleware overwrites it from the
verified principal (header trust remains only for legacy and secret-only requests until the cutover; the Preview
credential is always `viewer`). The main secret **without** a role header is the *machine*
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
`spend_budget_alert` row; no staff role sees or clears the tenant-only rows (`target_roles` {tenant}: the tenant's
bell, `GET /v1/notifications`, AA-986 L3). The UI mirrors the capabilities in `lib/rbac.ts` `ROLE_PERMS` / `can()` (kept
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

Status (S245, owner decision): the bridge is **kept on purpose**. Booking production signs staff in through
Cognito WORKFORCE (Booking roles from `booking-*` groups), but Vercel previews of the Booking admin have no
registered Cognito callback, so the legacy CIS password → `/auth/booking-token` path is still their only staff
sign-in. Removing these endpoints, the Booking bridge issuer and the signing key moves to a follow-up that first
designs a preview sign-in. Do not delete them as dead code.

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
revoked); plan and settings are unchanged, and cancelled jobs are listed, not re-enqueued. Tenant
data erasure is a separate step after offboarding plus a retention window, with explicit approval: the
`data_erasure` job (AA-839, see the job runner section) — DRY RUN by default, apply OFF behind
`ERASURE_APPLY_ENABLED`; after an apply the tenant cannot be restored.

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

- **RLS template** (229; also 237, 241, 262, 265): `ENABLE ROW LEVEL SECURITY` without FORCE (the owner `cis_owner` stays exempt; the
  main pool and the worker log in as `cis_app` since X1 S18 and read through `<table>_cis_app_all`, CIS 244), policy `tenant_isolation` `FOR ALL TO aa_app_user`, USING and WITH CHECK on
  `NULLIF(current_setting('app.tenant_id', true), '')::uuid` (`''` = no rows, never an error). A table
  without a tenant column goes through its parent: `acp_shared.angle_gate_option` matches options of
  `angle_gate_request` rows of the tenant (241), `acp_shared.quarter_plan_version` versions of the tenant's
  `quarter_plan` rows (265). `acp_deliver.tenant_tour_pages.tenant_id` is text: its policy compares text without a cast,
  plus `aa_app_user_aa_master_read` (the AA master's public pages, 239 shape). `acp_contract.tour_atoms` (266) is
  `FOR SELECT` only for the tenant role: `atoms_platform_read` (`owner_scope = 'platform'`) and `tenant_isolation`
  (`owner_scope` = the GUC, text); `catalog_owner_read USING (true)` keeps the catalog views reading every atom (D9:
  no live tenant atom on a live master tour, guarded at apply); TripPlanner holds no grant on it (CIS 254).
- **No privilege without RLS** (241; v2 since 265): `aa_app_user` holds no privilege, table or column level, on any
  relation without RLS. Two exceptions: the cross-app catalog contract, `SELECT` on the `catalog.v_*` views owned
  by `catalog_owner` (225 / 228), which other apps read too; and the **platform read allowlist** (265,
  `PLATFORM_READ_ALLOWLIST`): `SELECT` only, table level on `acp_contract.route`, `atom_segment`,
  `atom_segment_member`, `atom_segment_alias`, `acp_shared.place_card_alias`, `atom_place_card`, and column level
  on `acp_shared.place_card` (`card_id`, `status`: the staff `verified_by`, `cost_usd`, `naming_note` and `facts`
  stay unreadable), relations with no tenant column and no `owner_scope` (265 guards it), AA's shared derivation.
  `acp_contract` became reachable by the tenant role in 265, which therefore refuses any PUBLIC grant on a relation
  there without RLS and any SECURITY DEFINER function there the tenant role can execute.
  The `acp_contract` views (`v_active_tour_atoms`, `v_trip_registry`, owner rights) stay ungranted: tenant-pool
  code reads atoms through the base table with the 203 rule inlined. A new table the tenant pool needs gets RLS
  and its grants in the same migration. Enforced by `NO_PRIVILEGE_WITHOUT_RLS_SQL_V2`
  (`tests/integration/_rls_fixture_helpers.py`), guard 5 of 265 / 266; v1 (`NO_PRIVILEGE_WITHOUT_RLS_SQL`, 241's
  guard 4 and 262's guard 5, kept byte for byte) holds only before 265.
- **Catalogue-wide RLS guard (X2):** `tests/integration/test_rls_catalogue_guard.py` runs the rules in
  `tests/integration/rls_catalogue_rules.py` on the replayed migrations in every PR's Integration Tests job.
  The rules: a `tenant_id` table needs RLS and an `app.tenant_id` policy; no policy `TO PUBLIC`; no
  `app.current_tenant`; `current_setting('app.tenant_id')` only inside `NULLIF`; no FK across owners; no grant to
  PUBLIC; grants to another owner's role only on the list; `USING (true)` only on the X1 parity policies. Each
  exception is an `ALLOWLIST` entry with a one-line reason. A `tenant_id` table the tenant pool can read is never
  allowlisted. AA-CIS-Infra runs a byte copy (`db/rls_catalogue_rules.py`, Copy Guard) on the nightly Dev dump
  and writes the result into `db-schema-reference.md`. If you change the file here, copy it to Infra.
- Write grants are column grants where a route writes only some columns (`tenant_integrations`
  connect / test, `angle_gate_option.chosen`), so a tenant cannot move a row to another tenant or
  channel even inside its own rows.
- **Routes on the tenant pool** (flag in `api/core/aa544_tenant_pool.py`, `fail_closed=True`, one log
  line per request with `used_tenant_pool=`). All ON on Dev since S240 (16/10/2026), **except GET
  `/v1/publish-log`**: `aa544:publish_log_list_pool` was pinned to `"false"` in Redis before the S241 D-12
  merge (#792, to keep its pre-D-12 OFF default) and never switched back, so that route runs on the admin pool
  (Logs Insights 22–24/10/2026, AA-874). To end the pin: `SET aa544:publish_log_list_pool true`, confirm
  `publish_log_listed` with `used_tenant_pool=true`, then drop `publish_log_listed` from
  `tenant_pool_known_rollback_events` in AA-CIS-Infra `accounts/aa365/tenant_pool_alarms.tf` and this note.
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
  **Redis connection (AA-874).** Every client (api `app.state.redis`, worker resources, SEO cache) is built
  from `shared/cache/redis_conn.py` `redis_conn_args()`: `REDIS_HOST` (default `localhost`), `REDIS_PORT`
  (default 6379), `REDIS_TLS=true` → `rediss://` with the certificate and host name verified, and
  `REDIS_AUTH_TOKEN` (ECS secret from Secrets Manager) passed as `password=`, never in the URL. Without
  `REDIS_TLS` and token it is the plain `redis://<host>:6379` of the old node, so the image works on both; a
  token without TLS is dropped with one ERROR `aa874_redis_auth_without_tls`. The target node is an
  ElastiCache replication group with in-transit TLS (required), AUTH and at-rest encryption (AA-CIS-Infra).
  AA-CIS-Infra alarms (e-mail on ALARM and on OK) on any `aa817_tenant_flag_read_failed`,
  `aa817_tenant_flag_unrecognized`, `aa817_tenant_pool_unavailable`, `aa817_tenant_guc_not_applied` line and on
  any JSON line with `used_tenant_pool: false` (a route running on the admin pool, which RLS does not filter),
  except the events of routes rolled back on purpose listed in `tenant_pool_known_rollback_events` there (today
  `publish_log_listed`, the pin above). A deliberate rollback that is not listed holds that alarm in ALARM, and
  CloudWatch e-mails only on a state change: list it, or a second rollback goes unseen. Renaming any of these
  events silently disables its alarm.

  | Route(s) | Flag | Notes |
  |---|---|---|
  | GET `/v1/publish-log/pending` | `aa544:stage1:publish_pending_pool` | |
  | GET `/v1/content-writing/pieces/{id}` (+ `/export`) | `aa544:stage5:get_piece_pool` | |
  | GET `/v1/publish-log` | `aa544:publish_log_list_pool` | |
  | DELETE `/v1/integrations/{wordpress,facebook}` | `aa544:integration_disconnect_pool` | two short transactions around the Secrets Manager tombstone; a double click is 200/200 (`aa859_integration_disconnect_raced`) |
  | GET `/v1/content-writing/requests/{id}/latest-piece` | `aa817:content_piece_reads_pool` | |
  | PATCH `/v1/content-writing/pieces/{id}` (W2) | `aa859:piece_edit_pool` | audit after commit on MAIN |
  | POST `/v1/angle-gate/requests/{id}/choose`, `/reopen` (W3) | `aa859:angle_gate_decide_pool` | one transaction, request locked first (same order as the T8 worker); the response re-read follows `aa859:angle_gate_read_pool` (both on: same connection, same transaction); needs 241 |
  | DELETE `/v1/publish-log/{id}` (W4, unpublish) | `aa859:unpublish_pool` | three short blocks; Secrets Manager and the remote delete hold no connection |
  | GET `/v1/tours/pool`, `/my-versions`, `/versions/{id}` (W5a) | `aa859:t2_version_reads_pool` | job status from a second MAIN query keyed by job ids returned through RLS |
  | GET `/v1/tours/{id}/full` (tenant-JWT branch), `/{id}`, `/versions/{id}/export-docx` (D5b, S247) | `aa859:t2_version_detail_reads_pool` | own flag (not W5a's); the `/full` staff branch (X-Admin-Secret) stays on MAIN and reads no flag; export-docx audit on MAIN after the block; a master tour's raw / seo fields show only when those rows are the AA master tenant's (239), as in W5a |
  | PATCH `/v1/tours/versions/{id}` (W6) | `aa859:t2_version_write_pool` | `pg_advisory_xact_lock(859, hashtext(tenant:published_tour))` + `MAX + 1`; UNIQUE backstop → 409 `version_conflict` |
  | GET/POST `/v1/integrations/{wordpress,facebook}`, POST `.../test` (W9) | `aa859:integration_connect_pool` | Secrets Manager / remote HTTP hold no connection; the UPSERT writes only 241's column grants; needs 241 |
  | GET / POST `/admin/brand-identity` with a tenant Bearer (wave 3, S247) | `aa859:brand_identity_pool` | needs 262 (RLS on `tenant_brand_rules` / `tenant_seo_config`); POST = one transaction, `pg_advisory_xact_lock(859, hashtext(tenant:brand_identity))` + `MAX + 1`, UNIQUE backstop → 409 `version_conflict`; the X-Admin-Secret staff branch stays on MAIN and reads no flag; `check_saved_cta` on MAIN after commit |
  | POST `/v1/publish-log/{piece_id}/publish` (wave 3, S247) | `aa859:publish_pool` | three short blocks (piece + connection, `claim()` as a savepoint, `finish()`); Secrets Manager and the remote post hold no connection; daily-limit override read and audit row on MAIN; no new grant |
  | GET `/v1/angle-gate/requests/{id}` and the choose / reopen response re-read (wave 4, S247) | `aa859:angle_gate_read_pool` | needs 265; request + options + Subject header in one block; the t8_angles job status (`generation_failed`) on MAIN after it, only for `pending_goal` without a goal; with the decide flag on too the choose / reopen re-read runs on the decide's own connection inside its transaction (no second acquire that could 503 after a saved choice), otherwise its own sequential block. Choose / reopen fully off the tenant pool = BOTH keys false |
  | GET `/v1/slate` (wave 4) | `aa859:slate_reads_pool` | needs 265; enqueue (`request_slate_proposal`) and the tenant's channel config on MAIN first, last proposal on MAIN after; `POST /v1/slate/refresh` stays on MAIN |
  | POST `/v1/subjects/{id}/pick`, `/cut` (wave 4) | `aa859:subject_decide_pool` | needs 265 + 266; one transaction, the Subject row locked `FOR UPDATE` (pick) / one conditional UPDATE (cut); members read on the base table with the 203 rule; the pick audit on MAIN after commit, best-effort (flag off: inside the transaction, AA-559); a lock wait past the tenant pool's `lock_timeout` → 409 `subject_busy` |
  | GET `/v1/planning/trip-reallocation/suggest`, POST `.../confirm` (wave 4) | `aa859:trip_reallocation_pool` | needs 265 + 266; config on MAIN, one read block; confirm accept = one more transaction, `pg_advisory_xact_lock(859, hashtext(tenant:quarter_plan:year:quarter))`, save + approve together, UNIQUE backstop → 409 `version_conflict`, lock wait past `lock_timeout` → 409 `plan_busy`; audit on MAIN after the blocks for accept and reject, best-effort (flag off: audit first, as before) |
  | GET `/v1/marketplace` (wave 4) | `aa859:marketplace_pool` | needs 266; `posts_per_week` on MAIN first |
  | GET `/v1/content-writing/reviews`, `/requests/{id}/review` (wave 4) | `aa859:reviews_pool` | needs 265 + 266; one block each, no MAIN read; the trip page is a LATERAL (own page, else the AA master's) |
  | GET `/v1/notifications`, POST `/v1/notifications/read-all`, `/{id}/read` (AA-986 L3) | `aa986:tenant_notifications_pool` | needs 274 (RLS on `shared.notifications`: own rows written for tenants only; SELECT + UPDATE (is_read)) |

  A 241 rollback must `SET ... false` the two "needs 241" flags first (and list them in
  `TENANT_POOL_FORCE_ADMIN_FLAGS`), as 241's rollback comment STEP 0 says; a 262 rollback the same for
  `aa859:brand_identity_pool` (262's rollback comment STEP 0); a 265 / 266 rollback the same for the wave-4 flags
  that need it (their headers' STEP 0).

  **Wave-4 behaviour changes** (S247): the pick and the trip-reallocation confirm write their audit row on MAIN after
  the tenant transaction, best-effort, so a lost audit row no longer fails them (flag on only); confirm's save and
  approve are one transaction (a crash between the two no longer leaves an orphan pending version); the pick locks
  the Subject row, so a concurrent t7_slate proposal waits and then sees `picked`; the tenant trip query returns one
  row per tour with the tenant's own trip page, else the master's (it used to repeat a tour once per tenant with a
  page for it). The tenant route hub (`/v1/routes`, `/v1/hubs`, `/v1/route-picks`) is retired (0 callers, 0
  requests in 30 days); `acp_contract.route_pick` was quarantined (CIS 294) and dropped (CIS 296, AA-850).
- **MAIN exceptions** (code comment `# AA-859 MAIN exception: <reason>`): `write_audit_log`
  (`acp_shared.audit_log` has no RLS and is never granted; written after the tenant transaction
  commits, best-effort on the tenant path); enqueue-in-transaction routes (angle-gate goal, T2 rewrite /
  retry, content-writing write, slate refresh); `shared.job` status lookups (no tenant column); tenant
  self-config and usage reads (`/v1/quota`, `/v1/billing`, `shared.tenants` limits incl. publish's
  `daily_publish_limit`) keyed on the JWT `sub`; the tenant's planning config (`shared.tenants` /
  `acp_shared.tenant_config`, read first by the Slate and the trip-reallocation routes, D2) and the marketplace's
  `posts_per_week`; the angle-gate job status (`shared.job`, only for `pending_goal` without a goal);
  `check_saved_cta` → `notify_admins` after a brand save; staff branches (`/admin/brand-identity` with
  X-Admin-Secret, `/v1/tours/{id}/full` with X-Admin-Secret).
- **`/admin/atoms*` is staff only** (AA-859, S240): the AA-431 tenant-JWT branch was removed with no flag
  after 30 days of WAF logs showed 0 Bearer callers (1,041 calls, all `X-Admin-Secret`). A Bearer token
  gets 403 and logs `aa859_admin_atoms_tenant_refused`. No tenant route writes `acp_contract.tour_atoms`.
- **Sizing:** tenant pool `max_size` 2 per api task, api autoscaling max 7 (AA-CIS-Infra #156), so 14 ≤
  `aa_app_user` CONNECTION LIMIT 15. `aa859_tenant_pool_timing` (wait/hold ms) and the 60 s
  `aa859_main_pool_gauge` line are the inputs for any later resize. Since S247 the main pool is
  instrumented too (`api/core/aa859_main_pool_gauge.py::instrument_main_pool`): the gauge line carries the
  interval's `acquires` / `peak_in_use` / `waited` / `max_wait_ms` / `timeouts`, an acquire that waited
  >= 50 ms logs `aa859_main_pool_wait` (capped per interval), and an acquire with no timeout of its own gives
  up after 5 s (C2.3g) with ERROR `aa859_main_pool_acquire_timeout` (a 500, instead of hanging to the API Gateway
  30 s limit). Main pool size: the literal in `api/main.py` (source of truth, read by the Infra connection
  budget check). Per-role connections are alarmed
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

**Served state (tour liveness, AA-986, root ADR 0015)**:
Whether AA serves a tour, per catalog key (`raw_tours.source_key` = `COALESCE(source_group_id, tour_id)`):
`served` (live source version + live Master), `paused` (Master inactive, or a supersede waiting for the new
version's Master), `retired` (source deleted at S0/A0, Master trashed, or every version superseded). Derived, never
stored: read it from `gold_aa_internal.v_tour_liveness` (CIS 272). Every change goes through ONE write service,
`services/tour_lifecycle` (retire / restore source, supersede, set-active, Master pause / activate / trash /
restore, the publish insert): in one transaction it changes the status columns with the reason
(`published_tours.master_status_reason`), switches off any Master over a dead source (never two active Masters per
key), dismisses pending reviews, cancels queued T2 / T8 / T9 jobs, writes `gold_aa_internal.tour_lifecycle_event`
(append-only) and notifies every tenant with a derivative (`tour.discontinued` / `tour.reinstated`). A unit guard
(`tests/unit/test_aa986_l2_lifecycle_guard.py`) fails any other code that writes `source_status` / `master_status` /
`deleted_at` or inserts into `raw_tours` / `published_tours`. Retiring a published tour needs `master.admin` (Q8).
Tenants (L3): every tenant `/v1` row about a tour carries `served_status` / `served_reason` / `discontinued_at` /
`served_notice` (`shared/tour_liveness.py` `ServedState`; the portal badge reads `served_notice`: discontinued,
paused, or nothing for a supersede, Q2); new work on a non-served tour (rewrite, retry, T8 goal, T9 write, publish)
answers 409 `tour_discontinued` / `tour_paused`; the tenant bell reads its own rows through `GET /v1/notifications`.
Other apps (L4): `catalog.v_tour_retired` (tombstone) and `catalog.v_tour_lifecycle_outbox` (CIS 274, contract
`catalog.tour.v1.3`); `/v1/trip/{id}` answers 410 for a retired tour and 301 for an older version of a served one.
TripPlanner purge (AA-987): every lifecycle event gets one `tripplanner_purge` job (key
`tripplanner_purge:<event idempotency_key>`, enqueued after commit by `run_after_commit` and by the publish of
`process_export`, plus a sweep in the api ops sampler for any event without a job, last 2 h). The job re-reads the
event from `gold_aa_internal.tour_lifecycle_event` and POSTs it, HMAC-signed (`X-AA-Signature: t=..,v1=..`, secret
`aa-cis/<env>/tripplanner-revalidate-hmac`, same value as TripPlanner's Vercel env `CATALOG_REVALIDATE_SECRET`), to
`TRIPPLANNER_REVALIDATE_URL` (default `https://aa-tripplanner.vercel.app/api/revalidate/catalog`), which purges the
TripPlanner CDN / ISR caches. 4 attempts with backoff; not configured = succeeded `skipped: not_configured`.
_Avoid_: "delete" for a published tour (it is retired and can be restored); `raw_tours.lifecycle_stage` as a
deletion signal (it is the sales phase).

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
`atom_segment_alias` to the live id (an id that still has members is live, never followed). A Segment with no
reference left (AA-988: no live member, ranking, alias, Subject, brand-fit or route reference) is deleted by the
`segment_gc` recompute stage; it is re-minted with the same id if its place / verb comes back.
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
A blog request keeps a frozen copy of the Route's Segment ids (`angle_gate_request.route_segment_ids`).
Since AA-914 every reader of that copy (Slate pick, T8 angles, T9 write, admin regenerate) goes through
`services/acp_shared/route_guard.py`: a merged id is followed to its live Segment, and a Segment counts
only when the request's trip has a live member atom in it; every dropped id is logged
(`route_segment_dropped`). A pick or regenerate reads only the current Route version
(`superseded_at IS NULL`); a Subject still on a superseded Route is not pickable.
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
An automated check run inline right after Write (T9/T10). Since AA-924 (S250) a blog gets 23 gate
results, LinkedIn 22 and the other channels 21 (AA-787 had 25 / 24 / 23; count them in `run_quality_gates`).
AA-924 — **per-finding severity and two merged entries.** The gates still run as their legacy source checks;
the stored ledger folds F2_banned_patterns + F2_brand_forbidden + F15_voice's stock findings (STOCK_PATTERNS,
template lines) into one **F2_lexicon** entry (at F2_banned_patterns' place; one finding per word and sentence,
so a word on two lists is judged once; a longer phrase around a shorter word is a second finding), and F15_voice +
F15_voice_warn into one **F15_voice** entry. A merged entry carries `findings: [{text, severity: block|warn, source}]` (source = the legacy gate, a retired name),
`severity` (the worst), and `parts` (one legacy record per source); `violations` and `blocking` (worst is block)
stay for older readers. `quality_gates.expand_ledger` rebuilds the legacy entries and every routing step reads
that view (first failure, hold, sentence-repair plan, waive, regression guard keyed by source), so routing is
the legacy one; `held_reason` and `repair_log[].gate_targeted` name the stored entry (`F2_lexicon: …`). The
AA-800 layers stay their own gates and skip the tenant's own forbidden words. Ledgers stored before AA-924 keep
the old names and read as before. Since AA-613 each failure has a **severity**: `block` (product truth —
facts, grounding, CTA), `warn` (brand/style, e.g. generic AI wording, brand-fit) or `note`. Only
`block` triggers a rewrite (max 2 retries). Never assume "failed a gate" means "held" — check
the severity first. S233: blog shape gates F3/F5/F7 are `warn` (F7 auto-drops FAQ pairs that
restate the body). AA-787 (S245) removed `promises_an_option` (F17_trip_fact owns offered moments, ADR
0009), F8_framework (its LLM call), F3 and F5; F7 folded into F16_faq (the restate fixer runs before
the gates, logged as `F7_faq_autofix`). Old ledgers keep those names. S233 also added the blocking
gates F0_complete (cut-off text), F2_brand_forbidden (the tenant's own forbidden words; inside F2_lexicon since AA-924) and F11_brand_name (another brand / the platform speaking). An F9
judge outage is never a blocking gate (no rewrite on it), but since AA-925 (Q-2, reversing S233's "warn, not a
hold") it **holds** the piece: the F9 result carries a `degraded` marker and, when the FINAL attempt has it,
the last step after `_final_integrity` labels an approved piece `held` (`held_reason` "degraded:
f9_judge_unavailable"), adds a non-blocking `degraded` flag and a `repair_log` entry — see **Degraded
detectors** below. Count gate failures from `gate_ledger`, never `held_reason`
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
the SCOPE in title/meta/headings/body; warn by default — AA-788: `T10_F12_SCOPE_MODE=block` makes a body finding
block for one sentence repair and hold when it is left, a title/meta-only finding stays a warning) and
**F13_hygiene** (warn). Blogs also drop FAQ pairs the seed cannot answer (age, fitness, price…),
logged under `F7_faq_autofix`.
S234 (AA-771): LinkedIn has a **channel spec** by brand register (`services/acp_shared/channel_spec.py`:
consumer_social / editorial / warm_practical / observational, from the voice adjectives then brand_type) —
word band 120–250 (editorial 300), ≤3 hashtags, first line ≤140 chars; hygiene.autofix applies the hashtag cap
(each tag once — AA-780 S246) and first-line split. **F14_social_shape** (LinkedIn shape; warn, blocks above 1.3× the band → full
rewrite; AA-780: under the band's floor blocks on attempt 1 only → one full rewrite to the target (AA-788: the rewrite
is also given the source's fact sentences the post does not tell yet; kill switch `T10_F14_FLOOR_MODE=warn`), and never
holds a piece — `quality_gates.never_holds`; the AA-528 regression guard keeps a repairable rewrite of a floor-only attempt 1 for the attempt-3 repair, and on a revert waives the floor block), **F15_voice** (stock-phrase hard list; blocks on attempt 1 only, for one sentence repair, and
never holds a piece — `hold_failure` skips it; AA-780 S246: a STOCK_PATTERNS sentence still in the final attempt's text is cut in code after the loop, `service._cut_stock_lines` — never the hook or the CTA, never below the word band; shown as the non-blocking `F15_stock_cut` flag) and **F15_voice_warn** (trade register, audience-number leak,
generic or repeated opener, AA-780 broetry — ≥2 consecutive prose sentences of ≤5 words, bullets and emoji-only lines excluded; warn only, never repaired; stored as warn findings of F15_voice since AA-924, whose stock-list findings sit in F2_lexicon). All pass for every other channel. The F9 judge
gets the brand's voice adjectives and no longer favours a calm house voice. **F9_brand_style** (off-brand voice /
generic AI wording, from the same judge call) warns by default; AA-788: `T10_F9_BRAND_STYLE_MODE=repair` makes an
attempt-1 failure whose flagged phrase sits in a body sentence block for one `t9_repair` sentence repair, never a hold
(`STYLE_ONLY_GATES`). Each promoted check logs its decision (`t10_promoted_check`) when its mode is not the default;
`scripts/aa788/promotion_replay.py` measures the expected hold rate on stored pieces ($0).
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
S258 (AA-789 tier 1) — **seed alignment** (`services/acp_content_writing/seed_align/`): deterministic checks that
compare a sentence with the typed facts of its seed (`seed_graph.SeedGraph`: offered items, planned moments, 'near'
relations, world figures, the seed's own superlatives, brief lines). A finding needs a spaCy dependency structure
(`en_core_web_sm`, pinned, loaded lazily), a seed relation and a trigger word; any missing key abstains. Nine classes
(optional told as planned, planned told as optional, near told as at, difficulty promise, world share, scopeless
superlative, brief recipe, naming note, orphan opener). Each class has a mode (`T10_SA_MODE`, `T10_SA_MODE_<CLASS>`:
off / shadow / repair / hold, default off): shadow records candidates in `writer_input.seed_align` only; repair adds
`sa:<class>: …` findings to the existing F17_trip_fact / F18_rarity / F13_leak / F13_repeat results (no new gate);
`quality_gates.never_holds` keeps an `sa:`-only result from holding unless its class is in
`seed_align.modes.HOLD_PROMOTED` (empty: a promotion PR with shadow evidence, only the four fact classes). With every
class off the ledger is byte for byte what it was.
AA-1000 (tier 2) — **adjudicator** (`seed_align/adjudicate.py`): one LLM call per attempt (stage `t10_adjudicate`,
role judge, GPT-6 Luna on acc3, one `llm_call_log` row per reply) over the sentences tier 1 triggered on but left
alone (deterministic spaCy-free triggers; tier 1 must have run cleanly this attempt; ≤ 6 sentences), each with a seed
excerpt, for four forms tier 1 cannot reach: paraphrased naming note (F13_leak), implicit-subject repeat (F13_repeat),
open WHAT / WHY / HOW FAQ answer that does not answer (F13_repeat; answers of 2+ sentences only), hype such as "hard to
overstate" (F18_rarity). A VIOLATION counts only when its piece quote is in the sentence and its seed quote in the
excerpt and the seed. Modes `T10_ADJ_MODE` / `T10_ADJ_MODE_<CLASS>`: off (default; no call) / shadow (recorded in
`writer_input.seed_align[*].tier2`) / repair (at most ONE `sa:t2.<class>: …` finding per attempt → that gate's
sentence repair or cut); any other value is off. It never holds (`t2.` classes are never hold-capable), and a
result failed only by a tier-2 finding is no AA-528 regression (`service._regression_failures`); any model
error or the 60 s deadline changes nothing; a $0 replay (`f9_results`) calls nothing. Offline calibration:
`scripts/aa1000_adjudicate_calibrate.py` (hard cap $5).
S245 (AA-779) — **FAQ from People Also Ask, every answer names its subject.** The request's
`dfs_paa_snapshot.people_also_ask` (per tour) rides in the T9 context; the job keeps the questions of the
piece's own places (`quality_gates.paa_for_scope`: no out-of-scope place, no topic the source cannot answer,
names an own place or shares 2 seed words; ≤6) and gives them to the blog writer, the rewrite and `write_faq`
as SEARCH QUESTIONS (used only when a seed fact answers one). After the other FAQ droppers,
`drop_subjectless_faq` drops a pair whose answer does not name the question's subject (pronoun or
demonstrative opening, wrong subject) or only says what is not the case, while more than 3 pairs remain;
pairs it had to keep make the writer text's one FAQ regeneration rewrite them. Logged in `F7_faq_autofix`;
F16_faq stays the only FAQ hold.
S245 (AA-779 2A + 3) — **real-question source chain for blogs** (`services/acp_shared/real_questions.py`,
$0 DB reads, never a DataForSEO call): a blog's questions come first from its own Segments
(`atom_matches` kind `question` on the Segments' atoms — per Segment only the `questions_count` newest rows,
the table is upsert-only), then from `search_demand.people_also_ask` of keywords naming one of its places
(Segment and place questions English only — those can come from DE/FR/NL markets), then the tour-level PAA
of the request snapshot (a US/English SERP: not language-checked). T8 angles of a blog request read
the merged list (snapshot keeps the tour list as `tour_people_also_ask`); T9 `start_write` re-reads it live
with the piece's kept moments as scope. Off-meaning PAA (the place name used as a word — 'Is moron a bad
word?', or as a common noun — 'What is a moron?' — matched against the piece's own place words) is dropped in
the chain (every tier, so it never reaches the T8 prompt, ranking or the tenant-visible snapshot) and in
`paa_for_scope`. Each piece's `writer_input` (admin-only)
records `search_questions` (with source), `question_sources` (per-source counts) and `faq_questions` (each
final FAQ question's real source, or `writer`); the admin Content Quality drawer shows them. AA-779 (B) SERP
top-up and (C) Autocomplete are admin jobs that will add sources to `DB_SOURCES`.
S245 r7 follow-up (AA-779) — **real questions first.** The off-meaning test also drops a place name used as
a dictionary word ('Is moron a British word?', 'What is moron in German?', spelling / translation cues, or
the name lowercase with no travel word). When the writer's FAQ skips a given real question, the blog's one FAQ
regeneration (`write_faq` `required_questions`) answers it first; the FAQ then lists real-question pairs first
(a reorder only — a writer pair goes only above 5 pairs; deterministic, `real_first_faq`; `writer_input`
records `faq_pairs_before`/`after`). The lowercase-name test reads only whole single-word place names that are
not common English words, with no other place / country / travel word in the question, never on autocomplete
text. (B) is built: `services/acp_shared/question_topup.py` + `scripts/aa779_serp_topup.py`
(admin-run only, dry run by default, hard cap $3 across all runs, summed from `shared.dfs_call_log` endpoint
`serp_advanced_paa_deep`) buys one SERP with `people_also_ask_click_depth=2` per blog place with < 3 real
questions, query '<place> <country>', stored in `search_demand` (keyword `topup_keyword`, its own market
`US-PAA2` apart from the Segment research cache, no volume; an empty row marks a place bought with no
question) and read back as the `serp_topup` tier.
S248 (AA-982) — **T9 drops place PAA that A3 Jev already rejected.** On a blog write, `start_write` runs the
chain with `a3_filter=True`: the `place_paa` tier's questions are looked up in `shared.decision_log` under the
subject keys A3 wrote (`atom_ranking.scope_subject_key` / `landing_subject_key`) through
`decide.platform_verdicts()` (read-only: current wording hash, `NOT cached`, ≤ 180 days, platform tenant,
Zone recomputed with today's floors, acts only on enforce-mode confident verdicts; no Jev call, no new gate,
$0). A question goes when A3's own scope rule (`scope_dropped`) rejects it for the piece's country set or for a
Segment set that contains every piece country, or when `a3_landing_belongs` rejects it for every moment of
the piece (unless about_here accepts it for the piece). No verdict → kept. The dropped text cannot come back
through a later tier; `segment_match` is never filtered (it passed the same gates). Counts go to the
`t9_place_paa_a3_filter` log line (api, the write request) and `writer_input.a3_question_filter`. Kill switch: api env
`T9_A3_PAA_FILTER=off` (default on). T8 angles do not use it yet.
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
Quality registry (qtax-v1, AA-926): every gate, flag and gate log name is a constant in
`services/quality/registry.py`, with its label, class(es), kind (`gate` / `flag` / `telemetry`), channels and
`retired` flag in `services/quality/taxonomy.yaml`. The same file maps every Jev question key, admin chip and A1
validate / brand-audit / day-oracle / S1-first code-check code to an error class (17 classes + `other.*` at warn;
Writing Memory hard stop 1 = every `fact.*` class with `severity: hard`). `code_checks` names the detector modules
(`repair_contract`, `degraded`, `day_oracle`) and `aliases` maps each Q0 corpus class id (`other.length`,
`other.cta`, …) to its canonical class, so the Q0 cases are never relabelled to fit the taxonomy.
`python -m scripts.quality.redundancy_census` lists enforcing detectors whose classes another code check already
enforces (input for gate removals, AA-924). The 7.0 pass line and the other score cut-offs live only in
`services/quality/thresholds.yaml` (one `pass_line`, one key per consumer: A1 retry, judge, T2 status, T3, T7,
admin views, golden-set eval); Jev floors stay per question in `shared.decision_question`. Names are persisted
(ledger, `repair_log`, `held_reason`, flags, `llm_call_log.quality_signal`): never rename one; a removed gate stays
with `retired: true`. The admin UI reads labels, `PASS_LINE` and `SCORE_BANDS` from
`frontend/lib/quality-registry.generated.ts` (`python -m services.quality.gen_frontend`).
_Avoid_: check, validator, rule (Gate is the specific automated-check object; "rule" is generic).

**Piece**:
One row of generated content for one (Subject, Goal, Angle) — carries `attempt_number` (max 3;
attempt 3 is a sentence-level repair only, on the same row — the AA-485 buffer retry row is gone),
`gate_ledger` (every Gate's result on the persisted attempt), `status`
(`approved`/`held`/`failed`), and (blog only) a `publish_log` row once T11 ships it.
Each failed attempt's `repair_log` entry also keeps that attempt's own trimmed ledger
(`repair_log[].gate_ledger`: gate, passed, blocking, at most 5 violations; AA-791,
`gate_eval.ledger_snapshot`; AA-924: a merged entry also keeps `severity` and at most 5 `findings`
{text, severity, source}), so gate precision and recall can be measured across attempts; pieces
written before AA-791 have only the persisted attempt's ledger. A **Label** is an admin's human
verdict on one Piece (`acp_shared.content_piece_label`, migration 231, one row per Piece and
labeller: `as_is`/`light`/`heavy`/`reject`, per-class defects, minutes to fix, reason, and the
sha256 of the text labelled, so a later admin edit shows as changed). It is the ground truth gates
are re-scored against (AA-783 criteria); admin-only, set from the Content Quality drawer, no tenant
column and no `aa_app_user` grant.
`held` is an **admin-only quality label** (AA-770, S233): the tenant always receives the content
and can export and publish a held Piece like an approved one — including a piece held because a detector
was down (AA-925 `degraded`). **Tenants never see internal gate
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
Writers read both scopes for the trip's country, a tenant entry replacing the platform entries on
its subject (`facts.pick_facts`): T9 always (the FACTS seed, each entry with a `Source:` line the
leak checks know); T2 only with the worker env `T2_FACTS=on` (default off: a FACTS block in the
T2 user prompt, also read as T3 grounding source; `services/content_generation/t2_facts.py`).
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

### Writing memory (platform + tenant)

What the writers (A1/S1, T2, T9 blog, T9 social) learn from, under release control. Model weights
never change. See ADR 0010. The signal store `shared.writing_signal` exists since CIS 277 (AA-916, WM-03) and the
rule store `shared.writing_rule` since CIS 280 (AA-917, WM-09), the golden set and the eval run ledger since CIS 284
(AA-919, WM-17), the releases, the shadow outputs and the per-writer memory switch since CIS 287 (AA-919, WM-18 /
WM-20), and the pinned exemplars since CIS 313 (AA-921, WM-27).

**Writing Signal**:
One recorded piece of evidence about a writer's output: an admin sentence edit (before / after,
gate, problem), a tenant edit, or an approve / reject / regenerate, with its reason. Captured
silently; the tenant portal asks no feedback question (D7). Tenant signals live only in deletable
tables. Stored in `shared.writing_signal` (CIS 277): one row per signal, `scope` platform (A1, `tenant_id` NULL) or
tenant (T2 / T9, `tenant_id` set); RLS ENABLE + FORCE with `tenant_isolation` for `aa_app_user` (own tenant rows,
SELECT + INSERT only) and the X1 parity policies for `cis_app` / `cis_owner`, so every `cis_app` read filters
`tenant_id` explicitly. Tenant rows are deleted by the AA-839 tenant erasure (`services/erasure/inventory.py`);
`acp_shared.audit_log` keeps only the signal id and the sha256 hashes.
Captured by `services/writing_memory/signals.py` `record_writer_feedback` (AA-916) on: the tenant's T9 hand edit
(PATCH /v1/content-writing/pieces/{id}, best-effort, on the tenant pool), the admin A1 field edit (PATCH
/admin/tours/{tour_id}/generated/{content_id}, one signal per changed field), the admin sentence cut / rewrite / edit
of a T9 piece or a T2 version (with an optional reason code; `fact_not_in_source` on a T2 sentence also labels Jev
`a1_claim_supported` truth=false), and reject / dismiss / regenerate of a review row (reason-only, no text). The admin
history reads the text back from the signal (legacy audit rows keep their own text, D16). Reason codes and the chip
preselected for a gate: `services/writing_memory/reason_codes.py`. Every discarded T9 attempt is kept in
`content_piece.discarded_attempts`, and `writer_input.writer_prompt_version` stamps the T9 system prompt.
_Avoid_: lesson, feedback (alone), training data.

**Writing Rule**:
One learned constraint for a writer, of one type: `lexicon` (ban or replace a word or phrase),
`writer_instruction` (a line in the writer prompt), `repair_hint` (guidance for the repair pass)
or `preference_summary` (a tenant's ≤ 5-sentence voice summary; AA-921 WM-28, `services/writing_memory/preference.py`:
drafted on the judge route from that tenant's pass signals only, linted like a rule plus `unsourced_claim` — a promise
word, a specific or an add-verb quarantines it, stored `rejected` with `quarantine: true` — and released like any tenant
rule; the drafting is the `writing_memory_mine` job with `mode: preference_summary`, off unless the worker sets
`WRITING_MEMORY_PREF_SUMMARY`, else `needs_llm_path` and no call). A rule may only cut, ban,
rephrase or format; it never adds content, and fact gates (F1, F17, F9_cta_fact, Jev) outrank it.
Stored in `shared.writing_rule` (CIS 280): `scope` platform / tenant with the same CHECK and RLS shape as the signal
store (FORCE, `tenant_isolation` for `aa_app_user` but no tenant-pool privilege: tenants never see rules); status
`candidate` → `approved` → `active`, or `rejected` / `expired` (`expires_at` 60 days); approved / active need a human
approver (DB CHECK); one rule ≤ 25 words (a preference summary ≤ 5 sentences, tenant only); the approved + active
rules of a writer hold ≤ 400 tokens (platform) or ≤ 250 tokens per tenant (trigger `writing_rule_token_cap`); a
platform rule drawn from tenant evidence needs ≥ 3 tenants (`support_tenants`, D9). Provenance: `signal_ids`, support
counts, `lint`; the key that survives a gate rename is `reason_code` + `gate_class` (the qtax-v1 class). Tenant rules
go with the tenant (AA-839 erasure inventory).
Mined by the `writing_memory_mine` job (AA-917, `services/writing_memory/mining.py`): pass signals are clustered in
code ($0) by label — the reason code, else the gate's qtax class, plus every phrase a human edit removed — and a
cluster over threshold (tenant: ≥ 2 edits on ≥ 2 pieces; platform from tenant edits: ≥ 3 tenants; A1: ≥ 2
reviewers, or a gate failing on ≥ 5% of the last 30 days' A1 pieces; `services/writing_memory/config.py`) gets one
drafter call on stage `writing_memory_mine` (judge route, masked text only, ≤ 50 calls / ≤ $1 a run, ≤ $25 a
calendar month over every writing-memory stage). The draft is linted (`lint.py`: only constrains, a lexicon
replacement adds no specific, no 5-gram of tenant text or tenant brand in a platform rule, PREDICT: explains ≥ 2
held-out edits) and stored `candidate` (or `rejected` by the lint) with its provenance; a rejected draft is clamped
to the 280 limits, stored redacted when it held tenant text, and not redrafted until new signals join its cluster.
Automatic runs are off unless the worker sets `WRITING_MEMORY_AUTO_MINE`; when on: ≥ 10 new pass signals since the
last run, ≤ 2 a month, never during an open rerun wave. A manual run (admin, after a confirm step) is the 3rd. A
failed run that spent money counts as a run.
Signals reach `pass` through the fact filter (`writing_signal_fact_filter`, `fact_filter.py`, $0, daily from the
api ops sampler while signals are pending): an edit that adds a number, time, price or promise word the text and
the source lack fails (quarantined), an added name is grey (admin confirms), a style edit passes. The backfill
(`writing_signal_backfill`, `backfill.py`) copies the pre-AA-916 admin sentence audit rows, T2 tenant-edit versions,
T9 failed attempts (system signals, evidence for R0) and A1 brand-audit lessons, idempotently. Admin page
`/admin/writing-memory` (Signals and Candidates tabs, plus a Guide tab: `frontend/app/admin/writing-memory/guide.ts`
on the kit GuidePanel; `api/routers/admin_writing_memory.py`, admin only). A rule
reaches a writer through a release (AA-919), except an L0 lexicon rule (below).
**R0** (AA-918, `services/writing_memory/replay.py`, job `writing_memory_r0`, $0, no model call): the replay of one
rule over stored T9 texts (final text, discarded attempts, the writer's text before a human edit), gate ledgers,
repair logs and signals — how often it would fire, which stored failures quote its phrase (prevented), the signals it
matches, and whether its cluster recurred ≥ k in 30 days, k being the mining threshold of its basis
(`mining.cluster_threshold`, one function for both). The baseline sanity check re-runs the deterministic T10 gates on
stored texts against their stored ledgers (F9 verdicts passed through: `run_quality_gates(f9_results=…)`). The
result (counts and ids only) is kept in `lint.r0`, tied to the rule's text by `rule_sha`.
**L0 lexicon fast path** (AA-918, `services/writing_memory/l0.py`, ADR 0010 decision 6, D11): an `active` `lexicon`
rule of `t9_blog` / `t9_social` joins the brand pack's forbidden words at T9 `start_write` (prompt "Never use" line +
the F2_lexicon gate, source F2_brand_forbidden; a replacement also gets "Write X instead of Y" on the WORDS line), never
a post-gate substitution; skipped when it is a declared signature word, or in the CTA / brand name / trip name; the
piece's `writer_input.writing_rules` lists what it got. Activation = an R0 on the current text with `l0_ok` (phrase
drops to 0, no new hard stop, a replacement adds no number / place / time / price / service) + one admin click;
rollback = one click (`active` → `rejected`, `rollback: …`). No active rule: the pack and prompt are unchanged.
_Avoid_: lesson (the frozen AA-132 rows), output rule (the retired N8 table), guideline.

**Pinned Exemplar**:
One fixed, entity-masked example output (`[NAME]`, `[N]`, `[PRICE]`, `[TIME]`, `[URL]`) pinned in a Release and labelled
"voice only, reuse no fact". T2 and T9 get at most two per channel per Release; S1 gets none. No nearest-neighbour
retrieval. Stored since CIS 313 (AA-921, `services/writing_memory/exemplars.py`): the pool `shared.writing_exemplar`
(scope platform / tenant like the rule store, RLS ENABLE + FORCE, no tenant-pool privilege; status candidate / pinned /
never, each change with the admin's reason and an audit row) filled at $0 by the refresh from the "use as example"
signals (WM-07) whose fact filter passed (an approved T9 piece's text, or the edited T2 field, masked, whole sentences up
to 120 words; a "never use" signal excludes its source); `shared.writing_memory_release_exemplar` the links a `rules`
draft of T2 / T9 writes in its own transaction (pinned first, then the newest candidates; trigger
`writing_memory_release_exemplar_guard`: same owner, writer and channel, never a `never` one, never updated). The
examples are part of the release's `items_sha256` (`exemplars.release_sha256`; a release without examples keeps the
rules-only hash), loaded and checked by `insertion.attach_items`, rendered after the tenant rules (T9 user prompt, T2
block; never in the cached T9 `SYSTEM_PROMPT`), tenant first with the platform release's examples as fill, never
another tenant's. A copied 6-word run of an example is a QA trace `exemplar:<id>` (`eval_checks.exemplar_copies`) in R2,
the shadow and the post-activation check. Admin: the Exemplars tab of `/admin/writing-memory` and the Tenant 360 "Voice
memory" card (quality tab).
_Avoid_: few-shot (implies retrieval or unpinned examples), sample, golden item (an AA-729 eval item).

**Release**:
A versioned bundle of Writing Rules and Pinned Exemplars for one writer and Scope (`shared.writing_memory_release`,
CIS 287, AA-919 WM-18; ids only, the rule text stays in `writing_rule`). It passes the gates R0 ($0 replay of stored
text), R1 (re-score old outputs), R2 (golden-set run) and shadow, then becomes `active` only by one human click,
never automatically. Statuses `draft` → `evaluated` → `approved` → `active` → `retired` (+ `rejected`); a `reverify`
flag when the golden set, a model route or the gate taxonomy changed since its evidence (it keeps serving; a re-run
needs approval). Activation needs its evidence twice: the API (`services/writing_memory/releases.py`
`evidence_problem`, 409 with a code) and the DB (CHECK `writing_memory_release_evidence_check` + trigger
`writing_memory_release_guard`): an `empty` release needs R0 (0 byte diffs over ≥ 20 inputs) + the AA-729 baseline
eval; `lexicon_only` R0 only; `repair_hint_only` / `rules` an R2 pass + a shadow pass. One active release per writer
and owner (the pointer); rollback = the active release → `retired` (`rollback: …`, never re-activated) and the one it
superseded → `active`; no deploy. One regular release per writer and owner per month (the train; `train_closed_month`
on `shared.writing_memory_writer` closes it); lexicon, emergency (an open hard stop) and rollbacks never wait for it.
**Release 0** (`release_no` 0, class `empty`) renders nothing: the writers' prompts stay byte-identical (WM-20,
proved by `scripts/writing_memory/wm20_release0_replay.py` on Dev and `tests/unit/test_aa919_release0_replay.py` in
CI, and by `tests/unit/test_aa920_injection.py` through the DB resolve path).
The writers resolve their view once per write (`services/writing_memory/insertion.resolve`: the platform pointer +
the tenant's, explicit `tenant_id` filter, fail open, nothing when `memory_enabled` is off). The view carries the
release's rule items, loaded and checked once (`load_items`: every rule exists, belongs to the writer and owner, and
the items hash equals the release's `items_sha256`; otherwise the part renders nothing). **One builder** in
`insertion.py` renders every slot (AA-920): A1 HOUSE RULES after the forbidden-words line of the S1 writer v2
(platform rules only, no examples; the v1 / batch path stays frozen); T2 platform then tenant blocks at the end of
the system prompt; T9 platform rules appended to `SYSTEM_PROMPT` (cached across tenants) and tenant rules after the
brand words line, both filtered by the rule's `applies_to` channels; a `repair_hint` rule only in the repair pass of
a failed gate of its `gate_class` (A1 flag-fix, T9 `rewrite_with_feedback`), never in a writing slot. Activation by
writer (`releases.WIRED_CLASSES`): A1 and T9 take `rules` and `repair_hint_only`; T2 takes `rules` (WM-25; a T2
repair hint goes live inside a `rules` release); no writer takes `lexicon_only` (R0-only evidence; lexicon reaches T9
through L0 or inside a `rules` release) — anything else is 409 `content_not_wired`.
Each write records a stamp (ids, numbers and the release fingerprint `items_sha256`, never text): A1
`generated_content.metadata.writing_release` (S1 writer v2 only), T2 `tenant_tour_versions.generation_meta`
(`{writing_release, rule_ids, writer_prompt_version}`, CIS 297, admin-only: `_tenant_projection` hides the column and every
`generation_meta|rule_ids|release|memory` key from tenant reads) plus `shared.job.result.writing_release`, T9
`content_piece.writer_input.writing_release`. **Post-activation check** (AA-920 WM-26,
`services/writing_memory/post_activation.py`, job `writing_memory_post_activation`, $0, queued hourly by the api ops
sampler while a release with content is active; or POST `/admin/writing-memory/releases/{id}/post-activation-check`,
DRY RUN unless `apply`): the writes since the activation (the newest 200 per run) must carry the release's
fingerprint and hold no QA trace (`eval_checks.qa_artefacts`, the rule texts included) and no other tenant's text
(`isolation_probe`); `pass` after 20 carried writes. A violation rolls the pointer back at once (a tenant release
with no previous is retired alone; a platform one without a previous, or any refused rollback, turns the writer's
memory off), writes one audit row (ids, codes) and alerts (admin bell + `ops_alert_email` template
`writing_memory_rollback`). Routes `/admin/writing-memory/releases*` and `/writers*` (admin only).
**R2** (AA-919 WM-19, `services/writing_memory/r2.py`, job `writing_memory_r2`, CLI DRY RUN + `--enqueue`): a
candidate release (or today's writers) against the golden set: the judge calibrated on the frozen labelled items
(every run) and the stored controls re-judged by the current judge (stage `writing_memory_eval`, CIS 291; regenerated
only in a `reverify` run), then probe (<= 3, traps first) -> tune ->
holdout of the affected tiers, each candidate written through the real writer path (`eval_writers.py`; T9 = the first
write only) and checked for a new hard fact class (counts only when the judge's kappa vs the labels >= 0.6), a QA
trace and another tenant's text (`eval_checks.py`); fail-only early stop; hard cap <= $10 a run from llm_call_log.
**Shadow** (WM-21, `services/writing_memory/shadow.py`, job `writing_memory_shadow`): the candidate on recent real
inputs (20; T2 / Facebook 50; a social release for every channel 50 Facebook + 20 others; repair-hint-only <= 10; a
tenant release + 2 inputs of another tenant; golden tours excluded), QA-trace and
isolation checks, rows in `writing_memory_shadow`, cap <= $7. Every model call of both runs is written to
`shared.llm_call_log` under the eval stage with `quality_signal.eval_run` and tenant NULL (`call_log.bind_eval_stage`);
the run row is what the Releases tab attaches as `eval_run_id` / `shadow_run_id`, accepted only when the run names that
release (`release_id`; no code-train or seeded-fault run: API + CIS 292 trigger).
**Weekly KPIs and soft stops** (AA-922 WM-30, `services/writing_memory/kpi.py`, job `writing_memory_kpi_weekly`, $0,
queued every week by the api ops sampler for the last full ISO week; CIS 314): per writer (overall and per tenant id)
writes, re-flag with its Wilson 95% interval, repeat errors, light-edit share and minutes to fix (T9 labels), tenant
edits per write, reason capture (target 80%), cost per write, the replay hit rate, the rule funnel, the clean-pairs
counter (no export path) and the month's learning-loop spend; a rate with no data is None ("not measured"). For each
active release with content, its writes since activation against the 28 days before: a soft stop (re-flag Wilson lower
bound > 8% on >= 20 writes, light-edit share -10 points, minutes to fix +20%, tenant edits +20% and +5 points, cost
per write +20%; minimum samples apply) takes it out of service through the same path as the post-activation check
(`post_activation.move_pointer`: rollback, else retire alone / memory off; audit `writing_memory.release_soft_stop`, bell,
e-mail) unless a human recorded a keep (`shared.writing_memory_keep`: codes or all, <= 30 days; POST
`/admin/writing-memory/releases/{id}/keep`). Active rules past `expires_at` are re-confirmed for 60 days by a fresh
human pass signal of their cluster, else expire; the P3 project stop rule (two releases without improvement) is
flagged, never acted on. One stored report a week (`shared.writing_memory_kpi_report`, numbers and ids only); a DRY
RUN (`POST /admin/writing-memory/metrics/run`) stores and moves nothing. The **disagreement queue** (WM-31,
`services/writing_memory/disagreements.py`, `shared.writing_memory_disagreement`): up to 10 new items a week where an
R2 control verdict differs from the golden label, or a T9 piece the gates passed was labelled heavy / reject (or a held
one as_is / light); one-click resolution judge_right / human_right / unclear, audited. Admin: the Metrics tab of
`/admin/writing-memory`; trend: `scripts/writing_memory/wm30_kpi_trend.py`.
_Avoid_: fine-tune, model update, deploy (a Release is data, not code).

**Golden Set**:
A versioned, frozen set of labelled writer samples (AA-729: A1 50, T2 10, blog 15, LinkedIn 15, plus 10 injection
traps) that R2 runs a Release against. Stored since CIS 284 (AA-919, WM-17): `shared.eval_golden_set` (one row per
imported version, one active per name; platform inputs only — the `s1:*` brand pack), `shared.eval_golden_item` (one
sample: frozen input and output, label, writer / channel, scope / tenant, split `tune` / `holdout` (30%, stratified
by writer × label verdict, seeded) / `trap`, tiers incl. the derived `long_9d`, `fact_dense`, `post_edit`) and
`shared.eval_item_result` (one scored output per run, item, arm and mode). T2 / T9 items are tenant text (the tenant's
brand pack is inlined on its items): RLS ENABLE + FORCE, no tenant-pool privilege, deleted with the tenant (AA-839).
Runs are rows of `shared.prompt_eval_runs` with `pipeline = 'writing_memory'` and a `run_kind` (baseline_import,
r0_replay, r1, r2, reverify, probe, shadow); a trigger keeps controls honest (a human-edited output is never a writer
control; controls are regenerated only in a baseline or reverify run; a result belongs to a run of its item's set), and
a run with results keeps its writer, kind and set. Imported by `python -m services.golden_set.importer` (DRY RUN by
default, $0: model calls raise in that process; a new version must be newer than the active one). A CI guard
(`services/golden_set/text_guard.py`, keyed fingerprints in `.github/golden_text_fingerprints.txt`) fails when golden
text lands in this public repo; tests use invented fixtures only.
_Avoid_: test set (ambiguous), eval data, Pinned Exemplar (a release item, not an eval sample).

**Scope (writing memory)**:
`platform` (applies to every tenant's writer; never holds tenant text; needs evidence from ≥ 3
distinct tenants when the evidence is tenant content, plus Nghiệp's approval) or `tenant` (one
tenant's rules and exemplars, used only for that tenant, never for training). `tenant_id IS NULL`
iff `scope = 'platform'`, as in Facts Entry.
_Avoid_: global / local, shared (ambiguous with the `shared` schema).

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
  Ground truth for the Jev Question (AA-845): in the Review Queue drawer ("Source check") a reviewer
  marks single sentences unsupported (`decision_log.outcome` truth=false for that sentence only); a
  Review approve stamps truth=true on the rest, a Review reject stamps nothing (it judges the whole
  version). `GET /admin/decisions/claim-sample` rebuilds a stratified sentence sample (+ the source
  Jev read) for panel labelling ($0).
- **A1 day oracle + quality shadow** (AA-925, Quality System v1 Q-2): one duration / day-marker reader,
  `services/content_generation/day_parse.py` (`prompts._estimate_day_count` and `seo_meta_utils.tour_days`
  are thin wrappers; "3 hours" is one day, "7D/6N" seven). It also owns the ONE ranged-heading rule (AA-990
  S253, `day_range` / `CANONICAL_HEAD` / `canonical_blocks`): "Day 12-15" (hyphen, en dash or "to") covers days
  12..15 when 12 < 15 <= 12 + 14, else it is the single first day; source markers, written headings
  (`itinerary_utils`, `output_days`), validate's dash normaliser, the day gap check / `final_day_guard`,
  `places/validity`, `places/days`, the T2 day markers and the SQL twin `catalog._tour_day_text` /
  `v_tour.duration_days` (CIS 289) all apply it, so a ranged day is never folded into the day before.
  `validate_node` runs the deterministic day oracle
  (`services/quality/day_oracle.py`: sub-day split, duplicate / out-of-order / skipped / extra day, departure
  mid-trip, count vs duration, `SOURCE_DAY_CONTRADICTION` — a source fault, never charged to the writer —
  and `DAY_CHECK_SKIPPED`) and the S2 cut signature (`seo_meta_utils.meta_cut_signature`, "…a 20-kilometre.").
  Every code ships in **shadow**: it lands in `quality_shadow` (`generated_content.metadata.quality_shadow`)
  and changes no score or verdict. A code moves to enforce (`DAY_ORACLE_MODES`) only after a precision read on
  stored tours (`scripts/aa925/day_oracle_report.py`, D5: ≥ 0.70 with the Wilson bound); an enforced code is an
  A1 hard code (`_HARD_BLOCK_CODES`, rewrite class) and stays shadow on a tenant rewrite (T2/T3).
  A day list in the source ("Day 05 & 06", "Day 11, 12 & 13") is a range (S248). A meta ending in a two-word
  capitalised place name ("…and Hoi An.") is not a weak ending (S248).
- **S1 fact table + code checks** (S248, M2a S1-first step 1, Refs AA-645; offline only, not wired into the graph —
  S1-first freezes new gates): `services/quality/s1_facts.build_fact_table(tour)` builds, by code, the per-tour fact
  table the step-2 writer will read (duration kind, source days with their places, schedule lines, places, figures
  with unit / noun, included / excluded / optional lines, conflicts, the "ask the supplier" missing list, do-not-use
  operator names / contacts); every verbatim value points at its source field (`verify_pointers`).
  `services/quality/s1_checks.run_checks(tour, page)` measures a written S1 page against it (day oracle, day-place
  drift, cut / out-of-band meta, truncated body, novel figure, figure with the wrong unit, name not in source,
  inclusion the source excludes). `scripts/s1_step01/fixture_report.py` runs both over the golden fixture or any
  page set (A/B arms) and scores them against a ground-truth file; results with tour text go to the private root.
- **S1 writer v2 + A/B arms** (S248, M2a S1-first step 2, AA-645; offline only, the live graph does not import it
  — step 4 wires the A/B winner behind a flag): `services/content_generation/s1_writer_v2.py` builds, purely, the
  three arms the A/B compares — `aplus` (today's A1 writer prompt byte for byte + a code-built DAYS block), `w` (the
  new writer: a code-built FACT SHEET from `build_fact_table` instead of the raw dump; no per-day length targets,
  no nudge, no brand-differentiation block, no PAA; place background allowed, figures only from the sheet) and `e`
  (a code-built short page: At a glance box, `Day N —` blocks with source fact lines, Included / Not included; the
  model writes only name, subtitle, hook, highlights and one sentence a day). Every arm gets its SEO title + meta
  from a separate, code-checked call with one retry and no cut — at most 3 calls a tour. The day plan is code: source
  markers (ranges expanded) win over a disagreeing duration; a sub-day / one-day tour is one block.
  `raw_tours.group_size` / `best_time_to_go` reach the writer (`with_agency_fields`). The paid harness is
  `scripts/aa645/s1_screen.py` (LLMClient, `llm_call_log` stage `adhoc_aa645`, role `writer`; runs on the deployed
  image via S3) and `scripts/aa645/score_screen.py` (panel rubric v1 + rank, code gates, $ per tour, the W pick,
  paired Wilcoxon for the A/B runs); the gate codes are defined once in `scripts/aa645/gates.py`
  (DAY_PLACE_MISMATCH, NAME_NOT_IN_SOURCE, NUMBER_NOUN_MISMATCH are report-only). The A1 flag-fix prompt and the A1
  brand-audit judge's user prompt are neutral white-label (no "Adventure Asia"); the judge's voice rubric
  (`AA_BRAND_IDENTITY_PROMPT`) is unchanged until the step-5 cleanup.
- **S1 writer v2 on the live A1 path** (AA-645 step 0, S248; Nghiep d01 / d02): `S1_WRITER_V2` (env on the worker,
  default OFF) makes `generate_node` write arm W through `services/content_generation/s1_writer_v2_live.py` — writer
  + separate meta call + one meta retry on the `s1_generate` stage (the model is that stage's Model Gateway route:
  W x Sonnet = a route change in Settings, no model id in code), each reply one `llm_call_log` row (role `writer`,
  `quality_signal.writer_version = "v2"`). A1 admin rewrite only: never a T2 tenant rewrite (`is_tenant_rewrite`,
  stage `t2_generate`), never the Bedrock Batch seed path. The draft carries `writer_version = "v2"` (graph state,
  `generated_content.metadata.writer_version`): validate keeps ITINERARY_STILL_COMPRESSED and DFS_INTENT_UNDERUSED
  report-only for it (in `quality_shadow`, detector `s1_writer_v2`: v2 has no length target and no nudge, and uses a
  keyword only when it fits), and the A1 judge computes and logs brand fit but does not gate it (gate label
  `brand_fit_report_only`: no min() into the score, no tie-break, no feedback into the retry). The same holds on a
  reviewer edit / admin revalidate of a v2 version (`_revalidate_tour` seeds `writer_version` from the metadata).
  When the AA voice-rubric LLM brand audit runs on a v2 draft (only when the judge did not), its voice codes and a
  bare not-publish-ready verdict are report-only too (`quality_shadow`, detector `s1_writer_v2_brand_audit`); the
  pre-audit codes, product-truth codes and a `manual_check` verdict still gate. Every other gate (validate,
  grounding, flag_fix, revalidate, the day oracle) is unchanged. `raw_tours.group_size` /
  `best_time_to_go` reach the v2 fact sheet as `tour_agency` (A1 only).
- **A1 brand-fit rubric v2** (AA-645, S249; d02 follow-up): `services/content_generation/brand_fit_v2.py` scores the
  page's voice against the white-label house style WITHIN THE LIMITS OF ITS SOURCE — the judge reads the writer-v2
  fact sheet of the raw tour, plain wording on a thin source is not "generic", it never rewards or asks for brand
  colour / an angle / claims the source lacks, it lists the specifics the page states that the source does not
  support (quoted; code keeps only quotes found in the page and caps the score at 6, or 4 for three or more), and code
  caps it at 5 when the page names "Adventure Asia" / "Design This Journey" and the source does not. Same reply shape
  (`brand_fit_score` = voice, `cross_brand_distinct` = source specificity), same `s1_judge` route, same AA-714
  near-band median of 3 — `brand_fit.score_brand_fit(rubric="v2", source=tour)`. Selected by `A1_BRAND_FIT_RUBRIC`
  (worker env: `auto` default = v2 on a writer-v2 draft, v1 on today's writer; `v1` / `v2` force); a draft without its
  source in state falls back to v1. T2 and the T7 Debate stay on v1 (they never pass a rubric). The v2 score still does
  not gate a writer-v2 draft until `A1_V2_BRAND_FIT_GATE=enforce` (gate label `brand_fit_v2`, no Jev tie-break: the
  a1_brand_fit Question was set up on v1 scores); that flip waits for the offline re-measure on the 125 A/B pages
  (`scripts/aa645/brandfit_measure.py` + `brandfit_analyze.py`, pass = W x Sonnet retry % <= A+ retry % + 5 points AND
  Spearman(brand fit, panel serious errors) <= 0). Every v2 score carries its rubric version
  (`brand_fit_v2.RUBRIC_V2_VERSION`, pinned to the prompt text by a test) in `llm_call_log.quality_signal.rubric` and
  `generated_content.metadata.judge.rubric` (+ `unsupported`, `caps`); absent = v1. The two are different scales: the
  wave report labels each brand fit with its rubric. No brand-fit cache exists on the A1 path; the T7 Debate cache
  (`acp_shared.debate_brand_fit_cache`) holds `score_candidate_fit` scores, which this does not change.
- **Shadow run of the A1 chain** (AA-645, S249, d03): `scripts/aa645/shadow.py` runs `v1_pipeline._rewrite_tour` (the
  graph the A1 job runs, + the AA-736 Sonnet retry rule) with writer v2 on in-process only and its writer + meta calls
  on `--writer-model`, for a pilot (3 Laos tours with built places) or <= 50 tours stratified by country (golden and
  elephant-riding tours excluded). Nothing is persisted (no version, review row, publish, atomize, place work); the
  AA-729 isolation layer (`scripts/aa729/evalpatch`) logs every call under `adhoc_aa645` with the tour's job id, so real
  $ per tour comes from `llm_call_log`. `shadow_report.py` gives the per-country table and the memo section 6 stop
  conditions.
- **Per-wave quality report** (AA-645 step 0, d13): `scripts/aa645/wave_report.py` (read-only, $0) compares, per
  country, the version a rerun wave wrote with the version that was live before it: outcome (published / stayed v1 /
  not published), the A/B code checks on both pages (gate pages = the serious-error proxy, gate codes, figure
  coverage), stored validate codes, grounding still-unsupported, brand fit (report-only on v2), $ per tour, atoms after
  the wave (published-not-atomized, live atoms on a day past the new day count), and the memo section 6 stop
  conditions.
- **Degraded detectors + repair contract** (AA-925, Q-2, design §4.3/§6.3; `services/quality/`, pure modules —
  gate runners are passed in, no writer module is imported):
  - `degraded[]` (`services/quality/degraded.py`) names every fail-open path that ran on an output: Jev
    zone=error (`jev_unavailable` — hard on an enforce question or an unknown mode, counted on a shadow one;
    zone=skipped is not degraded), `grounding_not_run` (hard: the node crashed or the re-check failed; not a
    tenant rewrite, not the score-only revalidation graph), `grounding_no_source`, `f9_judge_unavailable`
    (hard, T9), `day_check_skipped`, `judge_unavailable`, `brand_audit_unavailable` (it already fails closed)
    and `repair_contract` (soft). Stored in `generated_content.metadata.degraded` (A1), the T3 outcome
    (`job.result`), `content_piece.flags` + `repair_log` (T9). One `quality_degraded` log event per output.
  - **One A1 hold**: `degraded.apply_a1_hold` at the end of the graph (`_apply_grounding_recheck`) adds the
    hard code `DEGRADED_DETECTOR` (`_HARD_BLOCK_CODES`, NOT the Sonnet rewrite class — a retry cannot fix an
    outage; `HARD_CODE_REPAIR` "review: re-run…") and `manual_check`; `_is_publishable`, `human_edit_gate` and
    the review summary read it through the hard-code path; the Review Queue shows it red.
  - A1 Jev today: `a1_claim_supported` has no reject ceiling, so Jev only CLEARS numeric hits — an outage is
    already fail-closed on numbers and the hold adds review load, not safety, until AA-845 sets a ceiling.
  - Per job: `decide()` notes every zone=error verdict into a contextvar collector bound by the job worker
    (`shared.job.result.degraded`), so A3 / ingestion / S1 prefetch outages are counted without their code.
  - **Repair contract** (`services/quality/repair_contract.py`): a repair may not fail a gate that passed,
    add a figure (or, for the grounding / T3 fact editors, a name) the text and source lack, cut a sentence
    short (incl. the cut signature "…a 20-kilometre." that `meta_complete_sentence` accepts — a modifier left
    without its noun; dropping a whole trailing clause before "and / with / to …" is the fact editor's job and
    is kept), touch an unflagged field, break a tag / bold / bracket pair, or (AA-645 `days_kept`) change the
    days the itinerary's line-initial "Day N" headings cover (ranges expanded) — drop, add or renumber a day, repeat
    one more often than before, or break the order (the S249 shadow: flag_fix's
    whole-itinerary rewrite returned Rajasthan with 10 of 15 days). The fit's fact tail may state the
    day count `tour_days()` derives ("8 nights" → 9 days). A violating field or sentence keeps its pre-repair
    text (soft `degraded: repair_contract`). Spliced at flag_fix (replaces the AA-641 per-field revert, same
    behaviour), the A1 grounding repair, revalidate's strip + fit, the T3 sentence repair + meta regenerate and
    T9 `splice_checked`. The T9 AA-528/AA-780 regression guard of full rewrites stays in
    `run_write_background` and already covers invariant 1 there. Invariant 7 (AA-790 `integrity_kept`,
    `enforce_step`): every deterministic edit of the T9 attempt loop (`hygiene.autofix`, the family caveat, the
    FAQ droppers, `real_first_faq`, `fix_trip_facts`, `trim_to_band`; `ensure_cta` is must-keep) is reverted when
    it leaves a new repeat, orphan, dangling opener, fragment, split or unfinished paragraph, or a sentence cut
    on 'the' / 'and' (`hygiene.integrity_problems` + `text_integrity.new_structure_findings`: a paragraph is
    unfinished only when it stops on a letter or digit, and paragraph kinds are new by count; steps before the
    final pass are compared as `text_integrity.settle` leaves them). The revert lines go to F13_autofix and the
    kept attempt's reverts count as soft `degraded: repair_contract` (detector `t9_step`); no gate or registry
    entry is added (gate freeze).
    The tenant-voice rubric (`acp_content_writing/voice_rubric.py`) is an offline scorer only
    (`scripts/aa790/voice_rubric_replay.py`): no pipeline reads it until the freeze ends and κ ≥ 0.6.
  - **Writer-v2 hard code** (AA-645, S249): on an A1 writer-v2 draft `ITINERARY_DAY_COUNT_MISMATCH` (a source day
    missing from the output) blocks Master — `graph.V2_HARD_BLOCK_CODES`, read through `graph.hard_block_codes(state)`
    by `_is_publishable` and the human-edit gate; v1 drafts and tenant rewrites keep the soft structure deduction. A
    meal fact line that copies the same source day's meal list ("Meals: dinner." from "(D)" / "Meals included:
    Dinner") is source-backed in the A1 brand pre-audit and is not sent to Jev. A page meal line where a negation sits
    next to a claimed meal ("dinner and no lunch"), or that names no meal and only says "included" / "all meals", is
    not source-backed and goes to Jev.
  - **AA-740 fitter**: `fit_seo_meta_final` no longer cuts a too-long meta mid-clause; it salvages at a
    sentence boundary, appends a facts-only clause to a short one, closes an uncut in-band clause, and
    otherwise leaves the length code to the LLM (A1 flag_fix seo_meta repair + `_rerepair_meta`; T3 a
    meta-only regenerate on `t2_flag_fix` before any whole-tour rewrite).
- **A2 — Admin QA Gate**: A1 rows that failed auto-validation (`status='hitl'`), reviewed via the
  Review Queue before they can reach A3. A row can also be **dismissed** (hidden without
  approving; a future real failure is enqueued again — AA-626, `review_status_enum` value
  `dismissed`).
- **A3 — Master Content Pool**: `gold_aa_internal.published_tours` — a tour is "in Master
  Content" once it lands here. Atomize (see Atom above) now fires automatically right after a
  tour is published here (AA-526, `services/export/handler.py::process_export()`), as a
  fire-and-forget background task, `owner_scope='platform'`.
  - **Publish dates** (AA-645 d06, CIS 273): `published_tours.published_at` is the FIRST publish — a new version of a
    published tour (re-export, a rerun wave, a promote) keeps it, so the tenant Browse pool and TripPlanner's browse
    order do not move; `published_tours.updated_at` records the last change, and `catalog.v_tour.updated_at` follows
    it (`catalog._live_tour.master_updated_at`). Anything that means "changed recently / in this wave" reads
    `updated_at`: the rerun-wave report (`services/waves/report.py`, `published_in_wave`), the admin library
    `published_last_30d` / `stale_count` / `last_published` and the overview funnel `published_7d`.
  - **Promote = per-tour rollback** (AA-645 step 0, C2): `POST /admin/tours/{id}/versions/{n}/promote` publishes an
    `approved` version through `process_export` (every text / SEO field, the score pointer, the live-source rule) and
    re-atomizes it with `dedupe=False` + `force=True` (the version's job key and day fingerprints are already spent);
    atom ids are content hashes, so the old text brings the old atoms back. A version that is not `approved` → 409
    (approve it in the Review Queue); a dead source → 409 `source_not_live`. The Master write is the tour lifecycle
    service's `publish` (AA-986 L2, see Served state): it locks the source FOR SHARE and re-checks it, so a retire that
    lands between promote's check and the publish is refused too (process_export's L1 refusal, 409). The publish-date
    rule above is the ON CONFLICT branch of the repository insert that only that service runs.
  - **Atomize retires what the new text no longer has** (AA-645 step 0): the per-day path soft-deletes live atoms on a
    day the itinerary no longer has (and day-less legacy rows), also on an all-skipped run (C6); the legacy path (no
    `Day N —` headers, random ids) soft-deletes the tour's earlier atoms after a good read (L1). An atomize that changed
    atoms drops the T7 Debate brand-fit cache rows (`acp_shared.debate_brand_fit_cache`) of the tour's Segments and
    the Routes through them (T13); the next Slate re-judges them.
  - **Accepted score pointer** `published_tours.quality_score_id` (FK to `silver_aa_internal.quality_scores`,
    ON DELETE SET NULL, CIS 268) = the published version's latest score row (`evaluated_at DESC, id DESC`).
    Export sets it with that rule; a revalidation (`_revalidate_tour`, `revalidate` job) writes the new score
    row, re-points the published tour and only then deletes the old rows, in one transaction (AA-983). The
    published `quality_score` number stays as exported. Rows left NULL before AA-983:
    `scripts/aa983_backfill_quality_score_id.py` (DRY RUN default, `--apply --expect N`).
  - The admin **Master Content** page reads `GET /admin/tenants/{aa_internal}/details`
    (`admin.py::get_tenant_details`, internal branch). AA-718 added server-side
    `page`/`page_size`/`sort`/`sort_dir`/`country`/`master_status`/`score`/`search` to it (the
    tour list was a client-side filter over a fixed `LIMIT 200`, which dropped the newest rows
    past 200). The summary now carries a filter-aware `catalog_total` + `avg_quality` (true
    `COUNT`/`AVG` over the same WHERE), a 30-day-windowed `llm_cost_window_usd` (reconciles with
    Cost Operations) alongside the all-time figure, and a `pagination` block. Each pipeline run
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

- **T0 — Brand Identity**: tenant sets up their own brand voice/rules (`/portal/brand`, was `t0-brand`),
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
  the rewrite (`/portal/tours`, was `t1-rewrite`; `PoolTab.tsx`'s "Rewrite" button).
- **T2 — Rewrite (execution)**: the LLM rewrite of the picked tour into the tenant's own brand
  voice. It has no dedicated UI route and runs as the durable `t2_rewrite` job on the worker (AA-652).
  - **Writer prompt (AA-792).** T2 has its own writer prompt: `prompts.T2_SYSTEM_PROMPT` plus a
    CLIENT BRAND block (`brand_pack.render_brand_block`), assembled by
    `batch_prompt.build_t2_system_prompt`. These builders are chosen only when `is_tenant_rewrite`.
    The user prompt uses `build_rewrite_prompt(voice="tenant")`. The tenant's core idea
    (`tenant_brand_rules.core_idea`) is the lens for the overview and each day's framing.
  - **Shared rules.** T2 keeps A1's truth and structure rules as the same prompt pieces: structure,
    source fidelity and the meta band. It drops A1's neutral catalogue voice. The A1 prompts are pinned
    byte-for-byte by `tests/unit/test_aa792_a1_prompt_golden.py`.
  - **Signature vocabulary (AA-800).** The forbidden words come in three layers
    (`services/acp_shared/signature_vocabulary.py`): platform truth (cheap, deal, discount, book now,
    instant booking) is refused for every tenant; AA style is unchanged for A1 but, for a tenant, stock
    superlatives (pristine, stunning, breathtaking, magical, paradise) are allowed once per tour (a second
    one is FORBIDDEN_WORD) and positioning words (curated, refined, tailored, bespoke, luxury) are allowed
    when the tenant declares them; the tenant's own list is always refused. A tenant declares a word in its
    T0 text (brand type, core idea, voice, style guide, on-brand example; not in a negated clause) or in
    `tenant_brand_rules.custom_validators.signature_words`. T2 prompt rule 1, validate, the strip, flag_fix,
    T3 and the admin T2 drawer all read the same layered list (`forbidden_words.state_forbidden`); A1 keeps
    `VALIDATE_FORBIDDEN`. Evidence rule: a service-level claim ("a luxury lodge", "a tailored itinerary")
    the source does not support is a T3 grounding finding (sentence repair), for every tenant. Rule 1
    nudges the writer to use a signature word where the source states that level (it names the source
    wording per word, the same roots the evidence rule accepts) and to leave it out everywhere else:
    source fidelity first.
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
    was billed but then failed (malformed JSON) is dropped from the verdict and gets its own
    `billed_unusable` row (AA-927); `judge_runs.failed_cost_usd` is informational — that cost is already
    in its own row, never add it again. When all three fail, each billed call still gets a
    `billed_unusable` row (as does a single failed call). Both repeat paths (AA-714 and median3) write
    one row per call since AA-927 — the verdict row carries run 1's cost, the rows of one verdict sum to
    the verdict's cost. Each call samples the stage-route shadow on its own, so a
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
    (`fit_seo_meta_final`) before a length code can trigger a rewrite: a sentence-boundary salvage or a fact
    clause (AA-925: no clause / word cut any more). The fact clause never adds an "in <country>" tail on a
    multi-country tour. A meta it cannot land gets ONE meta-only LLM regenerate (`_t3_meta_regenerate`,
    `t2_flag_fix`, under the repair contract) before a length code costs a whole-tour rewrite.
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
- **T4 — Pool**: the tenant's own rewritten-tours pool (`/portal/tours/mine` "My Tours", was `t4-pool`) — a T3-approved tour
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
- **T7 — Slate** (`/portal/create/social` "Social Posts" + `/portal/create/article` "Articles", was `t7-planning` "Social Content"): the tenant's ranked,
  per-Channel list of Subjects to pick from, read from the platform-wide Score/Route data (see
  Slate above). The older slot-grid UI on this page was removed (AA-519). Debate's brand-fit cut
  asks the Jev Question `t7_topic_fits_brand` before the Luna call on a cache miss (AA-700, shadow).
- **T8 — Angle Gate**: Goal + Angle selection (`/portal/create/write`, was `t8-angle-gate`; one continuous wizard
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
  as "City Palace" keeps its locality in the key and is never reused across tours by name; AA-784 S253: region is
  a first-level administrative division or empty and locality the settlement the place is IN or empty —
  `place_cards.clean_identity` clears geographic / trekking areas and the nearest town of a remote point before the
  check (round 3: remote = protected areas, summits / peaks / mountains / passes, camps / huts, glaciers, lakes
  outside a first-level city; a locality equal to the region is kept; a first-level city (`FIRST_LEVEL_CITIES`) is a
  right region, also of itself; a border-spanning place keeps a field only as both sides "A / B"; a person's life
  dates in parentheses are dropped from facts, at seed time too), and the check's `locality_ok` / `region_ok` clear a field instead of rejecting the card; present-tense
  status / administrative-role and authorship facts are never kept (`unverifiable_claim`, also applied to older
  cards' facts at seed time unless verified; the identity rules likewise to older cards' `Location:` line); a
  rejected card stored under a compound country (pre-AA-990) blocks a draft naming it (a hold only an admin
  lifts); `place_cards_build` with `{"card_ids": [...]}` re-drafts named cards
  in place — admin `POST /admin/content-quality/place-cards/rebuild`),
  `Day N`, same-country sibling evidence labelled "other trips", and a SCOPE line (which days and
  places the piece covers). S234: sibling lines only when the card has <3 facts (≤2, none naming
  another place); a Route's moments are in day order, skip Segments ranking excluded, carry no
  siblings, merge same-place moments into one block, and a moment with <3 facts is tagged thin (AA-789 tier 0: the
  thin note says "name it in passing" with no count or sentence shape, and the card's naming note reaches the writer
  only as a `Naming:` line of names, `atom_seed.naming_line`, never the note's prose). Facts Entries are filtered by `facts.country`; S233 removed the
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
  Word layers (AA-800, same module as T2): `F2_platform_truth` blocks (sentence repair),
  `F2_superlatives` blocks a second stock superlative (style only: one repair, never a hold),
  `F2_style_warn` warns on a single superlative or an undeclared positioning word; the writer prompt has a
  WORDS line built from the brand pack; an unsupported service-level claim is an `F17_trip_fact` finding.
  No Jev on T10: the AA-701 shadow questions were turned off in S234 (no signal) and their
  observer removed; migration 194's `t10_gates` question rows stay at `mode=off` as history.
  `/portal/content` (was `t10-review`) is now **My Content** (AA-614): the tenant's pieces with no gate/held
  details, displayed text is copy-locked (select/copy/right-click blocked, with a "Use Export"
  hint; the edit textarea stays editable). AA-669 migrated it to the UI-kit DataTable
  (columns/filters by channel·status·goal/sort/saved views/CSV) with a right-side drawer for the
  piece (content + context + flags + inline edit + exports), and bulk actions (export selected,
  publish selected eligible pieces) via react-query.
- **T11 — Publish / Export**: tenant-facing publish to WordPress (blog Channel) and a Facebook Page
  (`/portal/content/publish`, was `t11-publish`); the other Channels have no publish step built yet. The router
  `api/routers/v1_publish.py` keeps HTTP, auth, the tenant pool, Secrets Manager and the `publish_log` claim / finish
  and daily limit; the per-channel dispatch (integration type, adapter, post building, remote lookup and delete) is
  `services/acp_publish/dispatch.py` (AA-765). Blog markdown is
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
| 7 | Slate (Subject) | **Tenant** | Per tenant | **No.** `acp_shared.subject.tenant_id UUID NOT NULL` (migration 133:19). The similarly-named but unrelated `acp_contract.route_pick` (the Route-pick snapshot, renamed from `acp_contract.subject` at migration 132 — "a different, unrelated concept," migration 133:8) was also `tenant_id UUID NOT NULL` (migration 131:74, pre-rename); it was dropped by CIS 296 (AA-850). |
| 8 | Goal / Angle / Write / Gate / Review / Publish | **Tenant** | Per tenant, per request/Piece | **Mostly no, one deliberate exception.** `acp_shared.angle_gate_request.tenant_id` (migration 113:36), `content_piece.tenant_id` (migration 115:36), `publish_log.tenant_id` (migration 116:27) all `NOT NULL`. Exception: the T10 cannibalization Gate (F10) deliberately reads EVERY other tenant's approved Pieces — `find_similar_pieces(cross_tenant=True, ...)`, `_CROSS_TENANT_QUERY` has no `tenant_id` filter at all (`services/acp_shared/piece_similarity.py:46-56, 67-95`), consumed by `gate_cannibalization()` at the 0.92 cosine-similarity threshold. This is a cross-tenant **read/compare**, not a cache — the embedding itself is never shared: `compute_embedding()` (`services/acp_shared/content_embedding.py`) makes one fresh Bedrock call per Piece, every time, for every tenant, with no caching layer at all. |
| 9 | Facts Entry | **Both** (one table, two scopes) | Platform scope: once, by admin. Tenant scope: per tenant | **Yes for `scope='platform'` rows, by design.** `acp_shared.facts` (migration 145:33-46), `scope` column (`'platform'`\|`'tenant'`), `tenant_id IS NULL iff scope='platform'` (CHECK, migration 145:44-46). RLS (migration 229, AA-817, for the tenant pool role `aa_app_user`; the owner/main pool is exempt): `facts_platform_read` FOR SELECT `USING (scope = 'platform')` — platform rows are readable by every tenant — and `tenant_isolation` FOR ALL `USING`/`WITH CHECK (scope = 'tenant' AND tenant_id = NULLIF(app.tenant_id, '')::uuid)` — a tenant reads and writes only its own rows and can never write, delete or re-scope a platform fact (145's FOR ALL policy allowed that). |
| 10 | Writing memory (Writing Rules, Pinned Exemplars, Releases; ADR 0010) | **Both** (one model, two scopes) | Platform scope: per monthly Release, approved by Nghiệp. Tenant scope: per tenant Release, one human click | **Tenant scope: no** — every `cis_app` read filters `tenant_id = $1` explicitly and the tables carry the `tenant_isolation` RLS policy (FORCE RLS), following the `acp_shared.facts` pattern; a CI test proves tenant A's rules never resolve for tenant B. **Platform scope: yes, by design** — a platform rule may be drawn from ≥ 3 distinct tenants' evidence (D9) and then applies to every tenant's writer, but platform rows never hold tenant text. Tables are CIS-owned in `shared`: `writing_signal` exists (CIS 277, AA-916; isolation tests `tests/integration/test_aa916_migration_277.py`, `test_aa817_rls_tenant_isolation.py`) and `writing_rule` (CIS 280, AA-917; `tests/integration/test_aa917_migration_280.py`: no tenant-pool privilege, a `tenant_id = $1` read never resolves another tenant's rule), and the golden set's tenant items and results `eval_golden_item` / `eval_item_result` (CIS 284, AA-919; `tests/integration/test_aa919_migration_284.py`), the releases and shadow outputs `writing_memory_release` / `writing_memory_shadow` (CIS 287, AA-919 WM-18; `tests/integration/test_aa919_migration_287.py`: tenant A's release never resolves for tenant B), and the pinned exemplars `writing_exemplar` / `writing_memory_release_exemplar` (CIS 313, AA-921 WM-27; `tests/integration/test_aa921_exemplars.py`: as cis_app, tenant A's draft pins only A's examples and resolving for tenant B returns none of them; the link trigger refuses a cross-tenant link). |

**Retired predecessor (AA-915 → AA-923).** The AA-132 lessons loop (`shared.pipeline_lessons`) was
frozen by AA-915 behind `PIPELINE_LESSONS_ENABLED` and its code is gone since AA-923 (WM-34): no
reader, no writer, the S1 system prompt carries no lessons block, and `flag_fix_node._write_lessons_safe`
is a logging no-op kept only because the AA-729 harness patches it by name. The N8 output rules
(`acp_shared.acp_output_rules`, `/v1/rules`, `api/services/acp_post_processor.py`,
`services/acp_produce/rule_adapter.py`) went in the same change. Both tables move to the `retired`
quarantine schema by a later contract migration and are dropped after 14 days (DM-3.9); a unit test
(`tests/unit/test_aa923_retired_lesson_paths.py`) keeps every file from naming them again.
Learned rules reach a writer only through a writing-memory release (ADR 0010).

### Cross-tenant mechanisms — full inventory (AA-540's specific ask)

Since AA-545 the dividing line is simple: **everything up to Route/Hub is shared platform data;
everything from the Slate onward is per tenant.** The shared parts:

1. **The Master Content pool** (row 1) and **the Atom pool** (row 2) — the shared source every
   tenant reads from directly.
2. **Segment, Score, Route, Hub** (rows 3/5/6) — platform-wide rows, no `tenant_id`.
3. **Search Demand + its research-freshness log** (row 4) — a shared cache keyed on
   `(keyword, market)` / `(place, market)`, now also holding SERP domains (AA-631).
4. **Facts Entry, `scope='platform'` rows only** (row 9) — shared by explicit design, RLS-enforced.
5. **Writing memory, platform scope (ADR 0010)** (row 10) — platform Writing Rules may be drawn
   from ≥ 3 tenants' evidence and apply to every tenant's writer; they never hold tenant text, and
   each needs Nghiệp's approval. Tenant-scope rows are isolated.

One former **latent** cross-tenant path, retired: `shared.pipeline_lessons` (AA-132) has no
`tenant_id` and was written from the shared S1/T2 flag_fix node, so a working loop would have let
one tenant's T2 failures steer the global S1 prompt. Frozen by AA-915; its code was removed by
AA-923 (WM-34) and the table goes to quarantine.

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
- `docs/adr/0010-governed-writing-memory.md` — governed writing memory: release-gated rules and
  pinned examples, no fine-tuning; supersedes the AA-132 lessons loop (AA-915).
- `docs/implementation-notes/AA-526.md`, `AA-527.md`, `AA-529.md`, `AA-540.md` — build records
  for the A3 atomize move, the admin atom-curation page, Facts Entry, and this table
  respectively.
