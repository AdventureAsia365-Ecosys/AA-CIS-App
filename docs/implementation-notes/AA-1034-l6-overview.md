# AA-1034 R0 — the admin Overview drops its B2C cards (lane l6, CIS PR)

Root ADR 0019 (B2B-only ecosystem model, approved S265).

## Decisions

- The two B2C cards of the AA-858 business view are hidden: **TripPlanner customers** (`TripPlannerCard`: planners
  opened, trips started, sent to an advisor, AI searches) and **Booking activity** (`BookingFunnelCard`: traveller
  inquiries, trip cases, proposals, confirmations). Apps health and Cost per app stay.
- Hidden by a module constant `SHOW_B2C_CARDS = false` in `app/admin/overview/page.tsx` (not exported: a Next.js page
  may export only page fields). The components, `/admin/overview/ops` and the Booking / TripPlanner contract views stay,
  so the cards come back with one line if the decision is reversed; partner KPIs replace them in R1.
- The page description (EN and VI) no longer names "TripPlanner customers" and "the Booking funnel".

## Changed

- `frontend/app/admin/overview/page.tsx`, `frontend/app/admin/overview/messages.ts`.
- Test: `frontend/lib/overviewB2bOnly.test.mjs` (source guards, `npm run test:unit`); the staff smoke
  `tests/e2e/smoke/aa858-ops-overview.spec.ts` expects the health and cost cards and neither B2C card (touch hint test
  moved to the cost card).

## Tradeoffs

- The API still computes the two sections (cached 5 to 15 min): removing them is a later clean-up with the R1 partner
  KPIs, so this PR stays UI only and needs no backend deploy.

## Should know

- Booking admin's own dashboard B2C tile ("New inquiries") is hidden in the Booking PR of this lane.
- Live check: `/admin/overview` on the CIS admin shows Apps health and Cost per app, and no `ops-tripplanner` /
  `ops-booking` card (screenshot desktop + 390 px).
