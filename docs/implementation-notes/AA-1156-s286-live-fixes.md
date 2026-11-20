# AA-1156 — S286 live check F1: tile filters after a client-side click

Lane l9 of S286 (hotfix). Evidence: AA-Ecosys `docs/audits/2026-11-20-S286-session/live/evidence.md` (F1).

## Decisions

1. **Fix the shared hook, not only Place cards.** `useLinkFilters` read `window.location.search` once hydrated. On a
   Next `<Link>` navigation the new page renders before the browser URL changes, so the read saw My work's empty
   query. Supply copy and AA content only looked right because their query result re-rendered the same component and
   the second read caught the new URL; Place cards keeps the view in `Page` and the query in a child, so nothing
   re-read it (4,860 listed under `?status=auto_checked`). The hook now reads `useSearchParams()`, which follows the
   router URL on the first render of a client navigation and on a direct load.
2. **Place cards uses the hook.** Its view parse / query string moved to `frontend/lib/placeCardsView.ts` (pure, node
   tested); the page keeps `history.replaceState` for its own edits (the user's choice wins, as before).
3. **Suspense boundaries.** `useSearchParams` in a client page needs one for `next build`; the three pages wrap their
   body in `<Suspense>` (the Platform settings pattern).
4. **Hook signature.** `useLinkFilters(fromSearch)`: the `empty` argument only existed for the hydration guard.

## Changed

- `frontend/app/admin/_components/useLinkFilters.ts`, `places/cards/page.tsx`, `supply-content/{page,SupplyContentList}.tsx`,
  `aa-content/{page,AaContentConsole}.tsx`, `frontend/lib/placeCardsView.ts`.
- Tests: `frontend/lib/placeCardsView.test.mjs` (round trip; the hook uses `useSearchParams` and no
  `window.location`; every tile target list reads through the hook under a Suspense boundary), `myWork.test.mjs` test 5.
- Unrelated one-liner found by `npm run test:unit`: Tenant 360 lead route had no message for the API refusal
  `lead_email_not_public` (added with the agency TripPlanner module); EN + VI message added, `tenantLeadRoute.test.mjs`
  green again.

## Tradeoffs

- A plain `<a>` for the Place cards tile would also work (full load) but keeps the latent bug in the hook and costs a
  full page load; rejected.
- Other pages still have their own `readView()` from `window.location` (`admin/jobs`, `admin/tenant-qa/quality`). They
  are not My work targets; a client link with a query to them would show the same symptom. Left as a follow-up.

## Should know

- The frontend `lib/*.test.mjs` suite is not a required CI job in this repo (only the kit-consumers workflow runs it
  when kit core changes): run `npm run test:unit` locally.
