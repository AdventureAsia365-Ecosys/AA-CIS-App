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
- **Every number names its source** (`SourceHint`: info icon, `title` + `aria-label` "Source: …", focusable).
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
