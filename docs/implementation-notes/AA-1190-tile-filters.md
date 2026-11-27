# AA-1190 — My work tile links keep their filter on a click (S296 follow-up, CIS part)

Live check S296: two My work tiles opened their target page unfiltered after a click, while the same URL typed in
the address bar filtered correctly. The Review Queue (`?policy=none`, `?policy=elephant_riding`) and Data erasure
(`?tab=offboarding`) read `window.location.search` once in a lazy `useState`; on a client-side `<Link>` navigation
the new page renders before the browser URL changes, so they saw My work's empty query. Booking part:
AA-Booking `docs/implementation-notes/AA-1190-tile-filters.md`.

## Decisions

- **Review Queue: remount on a new link.** A wrapper reads `?policy=` and `?tour_id=` with `useSearchParams`
  (`reviewLinkFromSearch`, lib/reviewPolicy.ts) and renders the queue with `key = policy|tour_id`. The page never
  writes the URL, so only a navigation changes the key: Back / Forward, the menu link without a filter or another
  tile show what the URL asks for, with the paging and the other filters back at their defaults. The person's own
  picks hold while the link stays the same.
- **Data erasure: the URL is the view.** The tab and the selected DRY RUN / apply jobs come from
  `useLinkFilters` (lib/erasureView.ts parses and writes them). The page writes its view with
  `history.replaceState` (Next syncs `useSearchParams` with it), so no remount: the traveller hash kept only in
  memory for the verification run survives a tab switch. Writing the jobs now keeps `?tab=` (the old writer dropped
  every key but `job` and `apply`).
- **`useLinkFilters` follows the URL.** A pick now remembers the query it was made on; a different query (Back /
  Forward, a new link to the same page) shows the URL's filters again. Before, the first pick won for the life of the
  page. Supply copy, AA content and Place cards get this too.
- **Every other tile target was checked**: Settings, Compliance readiness, Supply copy, AA content and Place cards
  already read `useSearchParams`; Tenant QA compliance and Overview tiles carry no filter.

## Changed

- `frontend/app/admin/review/page.tsx` (+ `reviewModel.ts` re-export), `frontend/lib/reviewPolicy.ts`.
- `frontend/app/admin/tenants/data-erasure/DataErasure.tsx` + `page.tsx` (Suspense), new `frontend/lib/erasureView.ts`.
- `frontend/app/admin/_components/useLinkFilters.ts`.
- Tests: `frontend/lib/erasureView.test.mjs` (new), `lib/placeCardsView.test.mjs` (test 4: the page folder of every
  tile in MY_WORK_TILES reads no `window.location.search`, bar the listed URL writes), `lib/myWork.test.mjs`
  (source assertions moved to the new reads), `tests/e2e/admin-routes/my-work-tile-links.spec.ts` (new, runs in the
  Admin routes workflow).

## Tradeoffs

- The e2e spec compares, per tile, the API reads after a click with the reads of a direct load that carry the
  tile's filter pair; a filter that never reaches an API read (a tab, the settings "pending only" view, the policy
  select) is checked on screen. A new tile needs one of the two or the spec fails, on purpose.
- The source guard allows `window.location.search` in `overview/page.tsx` (URL writes in event handlers); other
  pages with the same lazy-read pattern that are not tile targets (Jobs, Tenant QA quality) are left for a follow-up.

## Should know

- Run the e2e spec locally as in `tests/e2e/admin-routes/admin-routes.spec.ts` (stub API + `next build`), project
  `admin-routes`.
