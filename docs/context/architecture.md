# Architecture at a glance

> Detail page of [CONTEXT.md](../../CONTEXT.md): the backend and frontend stack, the repo layout and the runtime services. Moved unchanged from CONTEXT.md on 24/11/2026
> (AA-1118); edit it here and keep CONTEXT.md short.

## Architecture at a glance

Architecture diagram (AWS icons): `AA-Ecosys/docs/architecture/diagrams/cis.svg`, drawn from `cis.py` in the
same folder; the catalog page `AA-Ecosys/docs/architecture/diagrams/README.md` holds every view and is shown in the CIS
admin at `/admin/docs?path=docs/architecture/diagrams/README.md`.

- **Backend**: Python (FastAPI-style routers under `api/routers/`, services under `services/`),
  running on **ECS** (cluster `aa-cis-dev-cluster`, service `aa-cis-dev-api`) fronted by ALB +
  API Gateway. Postgres (RDS) holds the schemas; some steps use Bedrock for LLM work.
- **Frontend**: one Next.js app here, `frontend/` = the **Admin** console (`app/admin/*`),
  `https://aa-cis.lumiguides.it.com`. AA-911 (root ADR 0019 d23 + S271 amendment): it runs on ECS as its own service
  `aa-cis-dev-admin` (container `cis-admin`) behind the shared ALB, workflow `Deploy Admin Dev` (see Deploy & CI).
  The Vercel project `aa-cis-frontend`, its Preview host and `vercel-deploy.yml` were retired in S272 (root runbook
  `docs/ops/runbooks/staff-web-ecs-cutover.md` section 4).
  - The **Tenant portal** (Content studio, `https://portal.lumiguides.it.com`) is NOT in this repo: it split off the
    admin as its own app in AA-885 (S243), moved to `AdventureAsia365-Ecosys/AA-Partner/portal` with its history
    in AA-979 (CRM-P2a), runs on Fargate behind CloudFront since the P2b cutover (27/10/2026), and the frozen copy
    left here was deleted at the P2b clean-up (runbook §4.5; `tests/unit/test_aa885_portal_split.py` keeps it
    gone). Read AA-Partner `CONTEXT.md` ("Portal") for its pages, shell, gate and sign-in. The Content studio API
    (`/v1/*`, `/auth/tenant-session`, `/auth/tenant-memberships`) stays here (root ADR 0010 §7, ADR 0010a).
    `/auth/tenant-login` answers **410 Gone** for any body: tenant API keys were removed (AA-1036 R3 slice 3, root
    ADR 0019 d6); there is no agency machine path to `/v1/*` until R8.
  - Admin pages: `dashboard`, `upload` (A0), `s1-rewrite`, `review` (A2 Review Queue — table
    view with a Dismiss action, AA-626; migrated to the UI kit + react-query with multi-select
    bulk regenerate and non-blocking regenerate, AA-662 PR 2 / AA-719), `master-content` (AA-718:
    server-side filter/sort/paginate + filter-aware stat cards + windowed cost + derived pipeline
    status, via react-query — see A3 note below), `atom-curation` (**Atoms & routes**, sections
    01 Atomize · 02 Segment · 03 Score · 04 Route/Hub; was "Social Content"), `tenant-qa/*` (AA-1039: `quality` =
    Content quality, `trace` = Content Trace, `review-log` = review log with gate telemetry per tenant and channel,
    AA-615, `slate` = the per-tenant Slate that was atom-curation section 05), `spend` (the **Spend** page, AA-622/623/665, was Cost Operations at `cost-ops`: tabs Overview, **AI & data spend** (views LLM, DataForSEO, Jev, from the ledger), AWS bill and By app (AA-1039 R6 slice 3, was `platform/cost`); the old `/admin/llm-usage` and `/admin/cost-ops` redirect to it), `lab/[stage]` (**Writing Lab**, AA-1039 R6 slice 3; tabs Eval gate, Samples, Labels, Experiments, Prompt versions, Releases and Metrics) `lab/writing-memory` (opens a tab from `?tab=`) and `lab/eval-runs` (**Eval runs**, AA-1169: runs, a run's samples, one
    sample side by side; the lineage reads),
    `ai-gateway` (**AI gateway**, was "AI providers", AA-804: tabs Providers (+ views Connections, Jev credit, Credit (AA-1025 B5); warm-path labels, the
    "Re-queue refused jobs" card; AA-1025 C1: errors and p50 / p95 latency per row over 24 h / 7 d from the ledgers,
    the Jev / DataForSEO / Geoapify switch (PATCH /admin/llm-connections/{key}), the partner CRM AI and Bedrock
    Guardrails rows from the CRM's daily totals (crm.ops.v1 GET /llm/usage only, with the staff member's own
    token; never the row ledger /llm/calls, which audits every agency), a static Mapbox row), Routing (with the LLM availability card), Models & trials (families, shadow report,
    the read-only Experiments section: prompt versions and eval runs), Budgets (every cap layer and the tenant AI
    budgets of AA-1016, also shown as a tile on Tenant 360), Calls (ledger v2 search), Guide (AA-1042 S270: the root page
    `docs/architecture/ai-gateway.md` / `.vi.md` (catalog id ARCH-01-ai-gateway, staff tier only) from the Docs sync bundle
    through `docs_reader.load_staff_page_by_id`, plus live tables from `GET /admin/ai-gateway/guide` (spend.read,
    `api/services/ai_gateway_guide.py`: routes with the serving deployment of `llm_route_control.serving`, model x
    connection matrix, connection register with secret NAMES only, connection caps, stage coverage); finance and
    ai_ops read that one docs page here (since AA-1133 they can open /admin/docs too, so its links lead there); AA-1064: the staff bundle images that page
    links are listed in `doc.assets` and served by `GET /admin/ai-gateway/guide/asset` (spend.read, only those paths)),
    and the Trace drawer
    opened from a Calls row, the job drawer, tour detail and decisions; Settings › LLM Models / Jev Credit and Cost Operations › Budgets moved
    there in UI part 2 and their old `?tab=` links are sent on in the browser), `tenants`,
    `jobs`, `decisions` (**Jev Decisions**, AA-660), `facts` (**Facts**, AA-814: platform Facts Entries the T9 writer cites; every staff role reads, `quality.write` edits), `photos` (**Photos**, AA-708 — Drive photo sync, unmatched queue), `api-docs` (**API Docs**,
    admin-only read-only OpenAPI reader built on the kit: `GET /admin/openapi.json` through the BFF, since the
    AA-577 `/docs`, `/redoc`, `/openapi.json` need the X-Admin-Secret header on ECS), `settings` (**Catalogue settings** since AA-1039 R6 rest:
    pipeline gates, the **AA house style** (Brand Identity editor of aa_internal), SEO config; the tenant row moved to
    its Tenant 360, the UI kit to `/admin/platform/ui-kit`). `run-health` was removed with N7/N8 (AA-603).
  - Admin navigation: see **Admin console layout** below (AA-1039, seven groups; AA-1037 adds Trade). Admin-only items are hidden for
    other roles (same list as `lib/rbac.ts` PAGE_RULES, enforced by `proxy.ts`). The legacy `(internal)` route group
    (`/upload`, `/review`, `/catalog`, `/brand`) is deleted; those URLs and `/admin/brand`
    redirect in `next.config.ts` (AA-663).
  - **The admin host and the portal (AA-885, AA-979)**: the admin answers `/portal`, `/portal/*` and `/tenant-login`
    with **308** to the same path on `https://portal.lumiguides.it.com` (`frontend/next.config.ts`, `PORTAL_BASE_URL`
    in `frontend/lib/apps.ts`) and serves no portal page or tenant BFF. Staff have **no** access to the tenant portal
    (no staff session there; AA-1035 slice 2 gives staff a read-only "view as partner" inside the admin instead,
    `/admin/tenants/view-as`); the staff login does not link to it.
    - No CORS is needed for the portal: its BFF calls the API server to server. The API sends no CORS grant on a
      deployed task (AA-876, `cors_allow_origins()` in `api/main.py`: empty when `AWS_EXECUTION_ENV` is set, only
      `localhost:3000/3001` off ECS); a browser preflight from any origin gets no `Access-Control-Allow-Origin`.
    - The admin app sends security headers on every response (AA-876; AA-1111 S284 after the ZAP baseline): HSTS
      (two years, sub-domains, no preload), `nosniff`, `Referrer-Policy`, `X-Frame-Options: DENY`,
      `Permissions-Policy`, `Cross-Origin-Opener-Policy: same-origin` (list in `frontend/lib/securityHeaders.ts`,
      sent by `next.config.ts`) and a full Content-Security-Policy: `default-src 'self'`, scripts only with a
      per-response nonce (`'strict-dynamic'`, no `'unsafe-inline'` for scripts), `style-src` keeps `'unsafe-inline'`
      (kit inline styles), images from any https host, `connect-src` = self + the Sentry DSN origin, `frame-ancestors
      'none'`. `proxy.ts` sends it with the nonce on every path but `/_next/static`, `/_next/image` and `/api/*`
      (the staff gate still runs only on `/admin/*` and `/login`); `app/layout.tsx` reads the nonce (`x-nonce`) for
      its restore scripts, which also renders every page per request; `/api/*` and `/_next/*` get the policy without
      a nonce from `next.config.ts`. `next.config.ts` redirects (`/upload`, `/portal`, ...) are answered before any
      header rule and carry none. `poweredByHeader: false`.
    - **Cross-site writes (AA-911):** every admin BFF write route (any method but GET/HEAD/OPTIONS)
      first calls `lib/crossSiteGuard.ts` `refuseCrossSiteWrite()` and answers 403 when `Origin` (or, without it,
      `Sec-Fetch-Site`) is not this origin. `it.com` is a public suffix, so every `*.lumiguides.it.com` host (the
      other staff and partner apps) is the same site and its requests carry the Lax session cookies. `tests/unit/test_aa911_bff_cross_site.py` keeps every write route on the guard.
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
    byte; AA-TripPlanner-Web takes the `tokens` profile, which since AA-1033 also carries `i18n.ts` and
    `aiNotice.messages.ts`). It holds `tokens.ts`, `brand.ts`, `primitives.tsx`,
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
    admin Platform → UI kit → Gallery; the gated gallery route of the visual job, S1d, will render it too),
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
    **AA-1069:** a kit button's test id is the `testId` prop; a plain `data-testid` attribute still reaches the DOM
    when `testId` is unset (it used to be overwritten with undefined), and `lib/kitButtonTestIds.test.mjs` (CI) refuses
    a `data-testid` attribute on any kit button call site (AST scan, same guard as the CRM web).
    **AA-1033:** `AiNotice.tsx` — the one notice under AI-written text in every app (`<p role="note">`, muted 12px
    line with an info icon), variants `staff` (default: CIS reviewer screens — Review Queue editor, S1 rewrite tab,
    the content-quality sentence rewrite panel when a model suggestion is loaded — and CRM AI drafts), `publisher` (Content studio review / publish / export) and `traveller` (TripPlanner). Its
    wording lives in `aiNotice.messages.ts` (`defineMessages`, EN + VI; says AI can make mistakes and that the
    confirmed quote and booking terms bind, never claims to exclude liability), which with `i18n.ts` is in the
    `tokens` profile so TripPlanner's own B2C notice renders the same sentence. Language: the I18nProvider's, or
    the `lang` prop (CRM web). Tested in `lib/kitAiNotice.test.mjs`; shown in the kit gallery.
    **AA-1040:** variant `narration` (a traveller reading what the AI wrote when they asked for it: TripPlanner trip
    narration, EU AI Act Art. 50(1)) and `form="badge"`: the short "AI-assisted" pill (`aiBadgeText`) for cards and
    rows, a `<span role="note">` whose accessible name and tooltip are the variant's sentence (Content studio).
    **AA-1131 (S283 UI audit K1):** `dataTableRows.tsx` (a DataTable row with `onRowClick` is a tab stop that opens on
    Enter / Space, with the kit focus ring; the page-size select is named "Rows per page"), `Drawer` is a named modal
    dialog (title or `ariaLabel`) with initial focus (`initialFocusRef`), a focus trap and focus return,
    `PageHeaderStyles.tsx` (the PageHeader layout rules, no longer copied into each app's CSS), and the PageFrame
    `<main>` is `position: relative`. The visual job checks accessible names (`tests/e2e/helpers/a11yNames.ts`: kit
    controls on every page, every control on the gallery) and drives the gallery's table + drawer from the keyboard.
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
    the app's lint. **Shell frame (AA-1008 S6e):** `core/ShellFrame.tsx` is the one app shell frame of every kit
    consumer (CIS admin, Booking admin, the AA-Partner agency app): layout [sidebar | topbar over the page], the phone
    drawer below 768 or 900 px (off-canvas, backdrop, Esc, out of the tab order while closed: its own hoisted style
    element keyed on `data-kit-shell`), the ⌘K palette state, bottom bar and footer slots, scroll "main" or
    "document", and `main="shell"` (it renders the `<main>`) or `"page"` (the page's PageFrame is the `<main>`); the
    nav, identity, sign-out and brand are the apps' own slot components. CIS-local, outside `core/`: `api.ts`,
    `guideContent.ts` (the guide checks, re-exporting the core shape), the shell configuration (`AdminShell`, a
    ShellFrame with `main="page"`; `Sidebar`, `Topbar`, `BottomNav`, `CommandPalette`, `SessionKeepAlive`), `adminNav.tsx`, `useAdminIdentity.ts`,
    `useReviewPendingCount.ts`, `chart.ts` (`CHART_TOOLTIP`, the recharts tooltip style, moved out of `adminUi.tsx`
    in AA-994 S6b), and `theme.ts` / `useTheme.ts` (the core store and hooks bound to `cis_theme`; the
    root layout's restore script is `themeStore.restoreScript`). Pages import the barrel `app/_kit` (which re-exports
    core), never `core/` directly; only the root layout and the login form import core files directly (server
    layout, light login bundle); a `messages.ts` may import `defineMessages` from `core/i18n`. `KX`
    (`app/_kit/adminTokens.ts`, AA-1006) names the few `--aa-*` colours the admin draws that core `K` does not
    (on-solid / on-dark text, the dark side surface, toast, scrim, shadows); it moves into core `K` when a second app
    needs one.
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
    from `app/_kit`. Every CIS admin page is on the kit (AA-1006, S263): the legacy `admin/_components/adminUi.tsx`
    is deleted and `frontend/eslint-suppressions.json` is empty, so any new kit-rule violation fails CI. The admin
    `_components` that remain (`auditPanels.tsx`, `FilterBar.tsx`, `Pagination.tsx`, …) are page parts built from
    kit components, not a second primitive layer. Living reference: the **UI kit page of Platform** (`/admin/platform/ui-kit`,
    admin only, was the UI Kit tab of Settings until AA-1039 R6 rest; `app/admin/_components/SettingsKitTab.tsx`) —
    renders every primitive with static data.
  - **Data fetching (AA-662)**: `@tanstack/react-query` is the standard for UI v2, wired app-wide
    via a single `QueryClientProvider` in `app/_kit/core/Providers.tsx`, mounted from the server root
    layout (`app/layout.tsx`) around all routes. Defaults: no refetch-on-focus, one retry, 30s
    stale. Pages that poll opt in per-query with `refetchInterval` (replaces the old hand-rolled
    `useState`/`useEffect`/`setInterval` fetch). Older pages keep their hand-rolled fetch until
    migrated.
