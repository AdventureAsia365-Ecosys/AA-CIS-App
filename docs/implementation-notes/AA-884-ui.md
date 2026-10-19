# AA-884 — PR D (frontend): Platform › Apps, Audit log, Cost per app

Branch `feat/aa884-platform-pages`, based on `feat/aa884-platform-apps-api` (PR C, #836) so the API contract
exists. Only `frontend/**`, `tests/e2e/**` and this notes file. Backend notes: `AA-884.md` (PR A/C).

## Decisions
- **Three pages, not two.** The task for this PR names `/admin/platform/cost` as its own page (the S243 plan had
  the cost table as a section of the Apps page). A page of its own keeps the Apps page one fast request (the cost
  GET may make a Cost Explorer call on a stale month) and gives the month picker room.
- **Nav.** People & access gets "Audit log" (`/admin/platform/audit`); a new Platform group "Apps" holds "Apps"
  (`/admin/platform/apps`); "Cost per app" (`/admin/platform/cost`) sits in "AI & spend" next to External Spend,
  where someone looking for money already goes. The `/admin/platform` index still redirects to Staff.
- **No rbac change.** `PAGE_RULES /admin/platform` and `API_READ_RULES "platform"` (admin only) already cover the
  three pages and their BFF reads. The "Refresh from AWS" button reuses `POST /api/admin/cost-explorer/check`
  (backend `spend.write`, admin only), the same call as External Spend.
- **Types in `lib/platform.ts`**, mirroring the backend services field by field (named in the file header), plus
  the small helpers the pages share (`shortSha`, `usd`, `auditQuery`, month helpers).
- **`APP_REGISTRY` in `lib/apps.ts`**: cis / booking / tripplanner with label, admin URL, repo URL and
  `commitUrl(sha)`. Dev constants, no new `NEXT_PUBLIC` env; `APP_LINKS` and `AppId` unchanged (a new
  `PlatformAppId` adds `cis`). The page prefers the backend's own `commit_url` and falls back to the registry.
- **No polling** (S240 Fluid CPU lesson): each page loads once on open (react-query, `staleTime` 60 s, no
  `refetchInterval`); Refresh on Apps sends `?refresh=1` once and writes the answer into the query cache.

- **Apps page** (`apps/AppsBoard.tsx`): one card per backend card id. CIS shows the API commit + task def revision,
  a parity badge (Same commit / Mismatch / Not compared, from `parity` true/false/null), each live worker with its
  commit (a "differs" badge when it is not the API's), the deploy gate (Safe to deploy / N active jobs + by status)
  and schema version vs required. Booking shows commit, `/health` and `/health/ready` HTTP code + status, DB and
  `bk-NNN`; an unreachable Booking says why (error class) and stays "unknown", as the backend does. TripPlanner
  shows today's online-stage spend vs the default cap with a bar (ok < 80 % ≤ warn < 100 % ≤ cap reached, the
  backend's `level`). A "Frontends" row links the three web frontends with "version not reported yet" (GĐ9 deferred).
- **Audit page** (`audit/AuditLog.tsx`): kit DataTable over server-filtered pages. Filters are applied on submit
  (a draft vs applied state), never per keystroke. Dates are whole UTC days: `since` = day 00:00Z, `until` = next
  day 00:00Z (the backend bound is exclusive). Keyset paging through `next_cursor` with a "Load 100 older rows"
  button (`useInfiniteQuery`); DataTable pages the loaded rows client-side (50 per page). Row click opens a Drawer
  with every column, the full actor, row hash and the details JSON. The chain badge reads `/audit/verify`
  (Chain verified / Chain broken at #n: problem), with pre-chain and unchained counts and the plain statement that
  the RDS master can still bypass the triggers until X1 (the chain makes it visible, not impossible).
  `admin:<uuid>` actors without a staff-name join are shortened to `admin:9d8c7b6a…` (full value in the tooltip and
  the drawer). Rows with `details.auth = legacy_unverified` carry a "Legacy" badge, explained above the table.
- **Cost page** (`cost/CostByApp.tsx`): month picker (`<input type="month">`, max = current UTC month), four stat
  tiles (total, AWS acc2, AI, untagged AWS with its share), a coverage line (covered_from – covered_to, last fetch,
  what the automatic refresh did), a kit DataTable by app (AWS by tag, AI, "AI already in AWS" subtracted, total),
  a totals strip, and "LLM spend by app" (bars) with the acc1 / acc3 satellite cross-check. Every number's source
  is the backend's `sources` text in a tooltip.

## Changed (vs. the plan)
- Notes file is `AA-884-ui.md` (task text), not `AA-884-frontend.md`.
- Cost per app is its own page (`/admin/platform/cost`), not a section of the Apps page (see Decisions).
- The Untagged row's per-service breakdown opens in a Drawer (row click) instead of an inline expanding row: the
  ESLint kit rule (AA-732) forbids a raw `<table>` in page code, and the kit DataTable has no row expansion. Any
  row with services or AI sources opens the same drawer.
- `ui-smoke.spec.ts` gets three entries (apps, audit, cost), not two. Apps and Cost set a `readySelector`
  (`platform-apps-ready`, `platform-cost-ready`) because they render cards / a summary before the table.

## Tradeoffs
- **"Refresh from AWS" = the existing `POST /admin/cost-explorer/check` for the month's window**
  (`start` = first day, `end` = the last included day). It fetches acc1/acc2/acc3 by service (External Spend's
  data) plus acc2 by App tag: 4 CE requests, about $0.04 per click — the button tooltip says so. A cheaper
  App-tag-only refresh would need a new write route (out of scope for a frontend PR); the GET already refreshes a
  stale month by itself at ~$0.01, at most about once a day.
- **No live polling anywhere.** The deploy gate and spend numbers are as of the page load (or Refresh); the page
  says "Built <time> (from the 60 s cache)".
- **Frontend SHA**: not shown (GĐ9 needs a build-time env var with underscores in CI, which Nghiệp edits by hand).

## Should know
- **Base**: `origin/feat/aa884-platform-apps-api` (PR C, #836) at `9717ff63`, i.e. after PR C was rebased onto
  main (which now holds #831, #832 portal split and #835 / CIS 248). Built first on PR C's pre-rebase head
  `acc3686b`, then moved with `git rebase --onto origin/feat/aa884-platform-apps-api acc3686b` (no conflict; the
  platform services / router / registry are byte-identical between the two heads). No vendored kit file
  (CONTEXT.md AA-885 list) is edited, so `test_aa885_portal_split.py` needs no portal copy.
- **Admin only.** No rbac change: PAGE_RULES `/admin/platform` and API_READ_RULES `platform` already cover the
  three pages; the nav hides them for other roles. The smoke entries are marked ADMIN-ONLY in a comment for the
  AA-878 viewer-mode smoke (it must expect a refusal there).
- **Local UI check** (no AWS, no DB): `next build` with `NEXT_PUBLIC_API_URL` pointed at a stdlib mock of the four
  backend routes (fixtures shaped like `platform_{apps,audit,cost}.py`, normal + degraded variants), `next start`,
  Playwright at 1440 and 390, light and dark, with scrollbars on (S239 lesson): ready markers present, no failing
  `/api/` call, no console error, `scrollWidth == innerWidth` and no element past the viewport outside a scroller,
  on all 24 page/viewport/theme/variant combinations; interactions checked (Refresh from AWS → outcome line and a
  cost refetch; Untagged drawer; audit drawer; Load older rows; filters → query `action=staff.&since=…T00:00:00Z
  &until=<next day>T00:00:00Z`; Apps Refresh → `?refresh=1`). The real check is the Vercel preview smoke + the
  Dev screenshots after PR C is live.
