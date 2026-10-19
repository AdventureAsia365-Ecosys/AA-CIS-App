# AA-858 — Ops overview UI (`/admin/overview` business view)

Branch `feat/aa858-ops-overview-ui`, stacked on `feat/aa858-ops-overview-api` (backend notes: `AA-858.md`).
Frontend only, plus one smoke spec.

## Decisions
- **Order (top → bottom):** Apps health strip, then TripPlanner customers and Booking funnel side by side (stacked
  below ~860 px), then Cost per app, then the heading "CIS content & operations" above the six AA-664 sections,
  which are unchanged (markup, test IDs, the 60 s refresh, the `overview` smoke entry).
- **Own query** `["admin-overview-ops"]` → `GET /api/admin/overview/ops`: `staleTime` 5 min, no `refetchInterval`,
  `refetchOnWindowFocus: false` (the data is AWS-backed and cached server-side). The Refresh button refetches both
  queries; the server cache still applies, so it never causes an AWS call per click.
- **Plain wording for Ms. Thư:** status pills Healthy / Worth a look / Needs attention / No data; KPI labels
  "Requests", "Errors", "Speed" (p95, "95% answered within"); Booking statuses mapped to plain names; AWS / Lambda /
  view names only inside the source hints.
- **Every number names its source** (`SourceHint`; since S243 built on the kit `InfoTip`, see the last section).
- **An unavailable section** (no IAM yet, view not applied, Redis down, request failed) shows a calm dashed note with
  the backend's plain `message`, never raw JSON or reason codes.
- **Charts:** recharts (already a dependency) with theme colours resolved by `useChartColors` (SVG attributes):
  24 hourly bars per app (successful vs server errors), 14-day TripPlanner bars. No daily cost chart (the cost module
  has no per-day breakdown; see `AA-858.md`). The cost card shows a share bar per row instead.
- **Links:** TripPlanner / Booking admin from `lib/apps.ts` (read only, no edit — AA-884 owns that file); Vercel Web
  Analytics of `aa-tripplanner` as a constant in `TripPlannerCard.tsx` (visits need a Vercel token the console must
  not hold, S241 §6; the link needs a Vercel login on the team); cost → `/admin/llm-usage` (External Spend).
- `opsFixture.ts` is a typed sample response (not imported by the page): local screenshots without AWS, and `tsc`
  keeps the UI types honest against the documented backend shape.

## Changed (vs the plan)
- No stacked daily cost chart (above). Funnel steps are a 4-tile grid with "N% of the step before" under each number,
  not chevrons (chevrons wrapped badly at 390 px).
- The new smoke spec lives in `tests/e2e/smoke/` (the `smoke` project only matches that folder), not `tests/e2e/`.

## Tradeoffs
- **Raw `<table>` instead of the kit `DataTable`** for the two summary tables (8 weeks, ≤ 8 cost rows): no search,
  paging or column menu is useful there, and the DataTable toolbar would be noise for a business reader. The lint
  rule is a warning; the existing AA-664 sections of the same page use raw tables too.
- **Phone (< 768 px):** tighter cells, the weekly table drops "Cancelled" (still a status chip) and the cost table drops
  the last-month column (last month's total is a note line under it). Measured on a local render with real scrollbars:
  table min-content 299 px / 259 px in a 326 px frame (≥ 20 px margin, lesson S239); page `scrollWidth` 384 ≤ 390.

## Should know
- **Merge after the backend PR is deployed**: the UI smoke fails on any 4xx from a same-origin `/api/` call, and
  `/api/admin/overview/ops` is a 404 on Dev until `feat/aa858-ops-overview-api` is live.
- `tests/e2e/smoke/aa858-ops-overview.spec.ts` is **admin-only** (the endpoint is `ops.read`): when AA-878 switches the
  smoke login to a viewer account, skip this file in viewer mode.
- Local check was a throwaway page rendering the cards from `opsFixture.ts` (not committed). It logs a hydration
  warning because that page server-renders the charts; the real page renders the cards only after the client query
  resolves, so it should not — confirm with the smoke's console-error check on the preview.

## S243 — the source of every number (Done-when: "each number traceable to its source in a tooltip")
Branch `feat/aa858-number-source-tooltips`. An audit of `/admin/overview` listed every number on the page: the four
business cards had hints, but several named tables, views, metric names and migration codes (finding 11 rule:
no table names or internal codes on screen), the Booking weekly columns used a native `title` only (no touch, no
keyboard), and none of the six CIS sections (pipeline, intelligence, tenants, jobs, AI and data providers) named a
source at all.

### Decisions
- **One kit primitive, `InfoTip`** (`app/_kit/primitives.tsx`, copied byte-identically to `portal/app/_kit/`, guard
  `test_aa885_portal_split.py`): a real `<button>` (accessible name from `label`) with the text in a
  `role="tooltip"` span tied by `aria-describedby` (always in the DOM, `hidden` while closed, so screen readers
  read it). Mouse: hover shows, click pins, second click / Escape / click elsewhere closes. Keyboard: Tab shows it
  (`:focus-visible` only, so a tap does not double-toggle), Enter/Space pins, Escape closes. Touch: tap toggles, tap
  elsewhere closes (hover handlers ignore non-mouse pointers). The bubble is `position: fixed`, clamped to the
  viewport and placed below (or above) its button, written straight to the element's style in a layout effect (no
  extra render, no set-state-in-effect); it follows its button on scroll / resize instead of closing (a keyboard
  focus that scrolls the page closed it in the first version). Fixed positioning means it is never clipped by a
  ScrollX table and never widens the page.
- `SourceHint` stays the page-level wrapper ("Source: …", label "Where this number comes from", test id
  `source-hint`), now on `InfoTip`; the native `title` is gone (it doubled the tooltip on hover).
- **All texts in `overview/_components/sources.ts`**: business words + the system + window + refresh, e.g.
  "AWS CloudWatch, CIS API requests, last 24 hours, refreshed every 5 minutes", "Booking database, trip cases
  opened …, total of the last 8 weeks", "AI calls log, all AI providers, this month (UTC)". The backend `sources`
  field (tables, metrics, migration codes) is no longer shown; it stays in the API as engineering detail.
- **CIS sections** got the same hints: each Stat tile, each number column header (pipeline 5, provider cost 3,
  quota 2), the three job numbers, the job status row (section title), AI this month, DataForSEO balance. The
  pipeline column hint explains the "+n/7d" delta.
- New small hints where a number had none: "Requests per hour" label above each sparkline, the alarm count line of
  each app tile ("N automatic checks …", plus "x of y firing" when some fire), the TripPlanner 14-day chart label.
- Labels with a hint are inline text (no flex row), so the (i) follows the last word when a label wraps at 390 px.
- Markers for the count check: `data-kpi` on every number tile (Kpi, Stat, Booking step tiles, job numbers) and
  `data-num-col` on every number column header.

### Changed (vs the issue)
- No frontend unit-test runner exists (no Jest/Vitest); the "component test" is a Python guard over the source
  file (`tests/unit/test_aa858_overview_sources.py`: plain-words regexes, no inline `SourceHint` text, InfoTip
  a11y markers) plus the e2e smoke (e) and (f).

### Tradeoffs
- Fixed positioning + reposition on every scroll event (capture) while one tooltip is open: cheap (one rect read),
  and only the open tooltip listens.
- The hidden tooltip text is part of the DOM, so `textContent` of a header includes it; `innerText` does not.

### Should know
- The UI Kit tab in admin Settings shows an `InfoTip` example. Frontend only (+ tests); no backend change, so no deploy order. The e2e smoke (a)–(f) passed against a local
  production build signed in to Dev as the E2E admin with no mocks; screenshots 1440 + 390 light, tooltips open by
  hover/click, tap and keyboard.
