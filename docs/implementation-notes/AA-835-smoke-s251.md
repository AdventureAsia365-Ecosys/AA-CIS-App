# AA-835 / AA-916 — admin smoke coverage and three UI fixes (S251)

The signed-in staff smoke (`tests/e2e/smoke/ui-smoke.spec.ts`, project `smoke`, run on the Production and fixed Preview
hosts) did not open the new Supply content pages (AA-835) or the Review Queue reason-chip modals (AA-916 WM-07). The
S251 screenshot review also found three UI defects.

## Decisions

- **Supply content list** joins the page list (`ADMIN_PAGES`, every staff role, matching `lib/rbac.ts` ALL_STAFF).
- **Not-found card**: a fresh random UUID per run; the page's own `GET /api/admin/supply-content/<id>` 404 is the expected
  answer, so `runPageCheck` gained `expectApiFailure` (exact path + status). It also drops Chromium's
  "Failed to load resource" console line for that same URL (`ConsoleError.source` = `msg.location().url`) and nothing
  else. The card text is asserted.
- **Review Queue modals**: Reject, Dismiss and Regenerate are opened on the first row that offers them, captured light
  and dark at 1440, and cancelled. A context route aborts every same-origin non-GET `/api/admin/**` call and the test
  fails if one was attempted, so the run can never act on the queue (Production staff smoke is read-only). Skipped
  for a role without `review.act` (the viewer: the page shows "View only") and when the queue has no rows; a missing
  Regenerate (no retryable row) is an annotation, not a failure.
- **Full-page screenshots were 1440x900**: the admin shell (`app/_kit/AdminShell.tsx`) is a 100vh column and each page's
  `<main class="aa-admin-main">` is the scroll region, so the document never grows and `fullPage: true` captures only
  the viewport. `screenshotMatrix` now grows the viewport height by what the tallest inner scroll region (at least half
  the viewport tall, `overflow-y: auto|scroll`) hides, up to 16 000 px, in at most three rounds, then resets to 1440x900.
- **Sidebar label**: "Supply content (Booking)" is cut with an ellipsis in the 236 px rail → "Supply (Booking)" (same
  shape as "AA content (CRM)"). A smoke test now fails on any visible `.aa-sidebar-label` with
  `scrollWidth > clientWidth` on the CIS Content section.
- **/login dark mode**: `LoginForm` pinned `data-theme="light"` on its wrapper. It now inherits `<html data-theme>`
  (set before paint by the restore script in `app/layout.tsx`: stored choice, else prefers-color-scheme) and calls
  `useThemeChoice()` so the theme store's matchMedia listener follows an OS switch while the page is open. Nothing is
  rendered from the hook's value (the server snapshot differs, an attribute would not hydrate cleanly). A public smoke
  test (no session, runs on every per-PR preview) checks the background luminance under light and dark.

## Changed

- `globals.css` comments: only the portal pins light now.

## Tradeoffs

- Growing the viewport also grows any `70vh`-style pane on a page; the shot then shows the page as it lays out at that
  height, which is what a reviewer wants from "full page".
- The label check runs on one section (CIS Content), not on every page, to keep a long label in another section from
  failing every page test; extend it per section when needed.

## Should know

- Not run in a browser locally (lane rule): tsc on the frontend and on the two smoke files, eslint on the changed
  frontend files and `playwright test --list` passed. The orchestrator re-checks on the preview / Production smoke.
