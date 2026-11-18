# AA-1133 — docs reader v2 follow-ups from the S284 live UI check (AA-CIS-App)

Lane l16-aa1133-reader-fixes, S284. Source: the live check of `/admin/docs` after the reader v2 PR shipped (root
`docs/audits/2026-11-18-S284-session/aa1133-docs-reader/`, scripts there drive the page). Three findings, fixed here;
the main note of the issue is `AA-1133.md`. The page history lane (another PR, `DocHistory.tsx`,
`api/services/docs_page_history.py`) is not touched; shared files (`page.tsx`, `DocsList.tsx`, `messages.ts`,
`lib/docsReader.ts`) are changed in other hunks than that PR's.

## Decisions

- **Who sees "Was this page helpful?" comes from the API.** The read-only Dev E2E session has the role cookie `admin`,
  so the page could not tell it from a real admin (`isReadOnlyRole` only knows the `viewer` role). Other admin pages
  solve this with an access answer computed by `staff_authz.denial_reason` on the write's own route (AI gateway
  `/access`, Tenant 360 members `/access`). The docs reader already loads the manifest once per visit, so the answer
  rides on it: `can_feedback` (`api/routers/admin_docs.py can_feedback`, denial_reason on `POST /admin/docs/feedback`).
  No extra request, and it follows the route policy exactly (viewer, both Dev E2E identities, unknown role, machine:
  false). An older API without the field: the role cookie decides as before (`lib/docsFeedback.ts feedbackAllowed`).
- **Refusals in words.** `feedbackRefusal` maps the API answer to a message key: read-only (403 `e2e_read_only` or
  "Role 'x' is read-only"), not allowed (any other 403), page gone (404), invalid (422), unavailable (503), failed
  (anything else). The raw message is never shown. After a read-only refusal the buttons stay disabled.
- **Scroll-spy recomputes on scroll, scroll end, resize and hash change.** The old hook ran only on
  IntersectionObserver callbacks (rootMargin -70% bottom); once a heading was inside the band no callback came, so a
  heading scrolled to 16 px (its scroll margin) after a click was never marked and the previous one stayed. The new hook
  listens to `scroll` (capture, so any scrolling box counts; at most one pick per animation frame), `scrollend` (where
  the browser has it), `resize` and `hashchange`; the pick itself is pure (`lib/docsToc.ts pickActiveIndex`).
- **The heading the reader went to stays marked while on screen.** A click in the table of contents, a `#doc-<slug>`
  address or a search hit pins that heading: a short last section can never reach the 96 px line, and the reader still
  sees the entry they clicked marked. Wheel, touch or a key press ends the pin (the reader scrolls by hand).
- **The address carries the heading.** A table of contents click replaces the hash (`history.replaceState`, no new
  history entry); opening a page at a heading (search hit, a link to another page's heading) pushes
  `?path=…#doc-<slug>`, so the place can be shared.
- **A fresh load of a shared heading address jumps to it (review fix).** The jump used to come only from in-app
  clicks (`open` set it), and the browser's own fragment scroll runs before the page text exists, so a shared
  `?path=…#doc-<slug>` link opened at the top. The page now seeds the jump from the address once
  (`lib/docsToc.ts addressHeading`, lazy `useState`; "" on the server, so no hydration difference: the value is not
  rendered). The jump itself moved from `Reader` into `ReaderColumns` and waits until the reader is measured: the first
  render puts the table of contents above the text, and a jump made then would land too low once it moves to the side.
  A measured reader also means the jump no longer repeats when the same page's data is refetched.
- **Search list polish.** While hits show, the "N of M pages" bar (and its Expand / Collapse all) is hidden: the hits
  have their own count and the groups are not shown. Hits are reordered only inside a page / translation pair (the pair
  keeps its best server rank; the copy in the reader's language first, nothing dropped: `lib/docsSearchView.ts
  rankByLanguage`). A section whose snippet has no words gets no row.
- **"Reader's language" = the page language the reader reads in.** That is the EN / VI choice remembered on this
  browser, else the admin UI language (AA-994), the same value that picks which copy a page opens in. A reader who
  never touched the switch gets the UI language, as the finding asked.

## Changed

- Backend: `GET /admin/docs/manifest` gains `can_feedback` (boolean). No new route, no migration, no POLICY change
  (JSONResponse, so the OpenAPI snapshot does not change).
- Frontend: `DocFeedback` (refusal text, lock), `DocToc` (new hook), `DocSearchResults` (ranking, empty sections),
  `DocsList` (bar hidden during search), `page.tsx` (can_feedback, hash in the address). New pure modules with node
  tests: `lib/docsToc.ts`, `lib/docsFeedback.ts`, `lib/docsSearchView.ts`. The old `feedbackFailed(msg)` message is
  replaced by six `feedbackErr*` messages (EN + VI).

## Tradeoffs

- **A field on the manifest vs a `/admin/docs/access` route.** A separate route would match the AI gateway pattern
  more literally but costs a request per visit for one boolean; the manifest is already per caller (owner pages).
- **Section without snippet: omitted, not "first words".** The search index has no body text for such a section (the
  FTS5 snippet of the body column is empty only when the body is empty), so there are no first words to show; the
  page title row still shows the hit.
- **Pairing by file name.** Search hits carry no `translation_of`; the pair key strips `.<lang>.md`, the naming rule
  of every translation (root docs/STYLE.md). A page that broke the rule would simply not be paired (still listed).
- **Pin ends on user input, not on leaving the screen.** Ending it when the pinned heading leaves the screen would end
  it during the smooth scroll towards it. A scrollbar drag does not end the pin, but the pick still falls back to the
  position as soon as the pinned heading is off screen.

## Should know

- The 96 px line, the 8 px pin slack and the one-frame throttle are layout constants (`technical-constant`), not
  policy thresholds, so no admin setting.
- Shared heading link: the smoke spec `tests/e2e/smoke/aa1133-docs-shared-heading.spec.ts` (@staff, runs after the
  Dev deploy) opens the catalog page at a later heading and checks it is on screen and marked. A page whose Mermaid
  diagrams above the heading finish drawing after the jump can still shift it down; the pin keeps the mark while the
  heading stays on screen.
- `scrollend` is missing in older Safari; the per-frame scroll pick covers it (the last frame of a scroll is its end).
- Live check: sign in as the Dev E2E admin, open a docs page: no "Was this page helpful?" section; the manifest JSON has
  `"can_feedback": false`. As a real staff role it is `true` and the widget shows.
