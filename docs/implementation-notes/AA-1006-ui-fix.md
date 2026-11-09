# AA-1006 — follow-up fixes from the S263 production UI check (lane ui-fix)

The S263 check of the kit admin pages (AA-1006, CIS #1226) on production found four issues. This lane fixes them in
one PR: three are frontend only, the Content Trace one also needs the content-log API to page and count. Kit
components only; no kit core file changes, so no kit sync PR in Booking / Partner / TripPlanner.

| # | Page | Finding | Fix |
|---|---|---|---|
| F1 | `/admin/overview` | Alerts card 65 px wider than a 390 px phone: a long alert key (`tenant_ai_budget:<uuid>`) and the date in one nowrap flex row | `_components/AlertList.tsx`: the key wraps anywhere (`min-width: 0`, `overflow-wrap: anywhere`), the date stays whole, the row stacks on a phone and wraps on a narrow desktop card |
| F2 | `/admin/master-content`, `/admin/upload` | DataTable toolbar "Columns" button flush against / clipped at the right edge | the table block sits at the inset of the header above it: Master Content 36 px (16 px on a phone), Upload's flush cards 20 px (`TableInset`) |
| F3 | `/admin/tenant-activity` | 10 columns wider than the pane at 1440; "Total pieces 200" was the number of loaded rows; no pager | API `offset` + real `total` / `by_channel`; kit DataTable server paging (50 a page); 7 columns with wrap widths |
| F4 | `/admin/master-content` | During "Refreshing…" the previous page's rows were numbered with the next page's range | row numbers, range text and pager follow the answer's own `pagination` (`lib/pageRange.ts`); the old rows are dimmed |

## Decisions

- **F1 stacks by `useIsPhone()`** (kit, the 768 px phone cut the admin shell and DataTable cards use) rather than a
  CSS media query: the admin pages are inline-styled, and the same hook drives every other phone layout. The desktop
  row also gets `flex-wrap: wrap`, so a narrow card wraps instead of overflowing.
- **F2 fixes the pages, not `DataTable`.** The kit table draws its own bordered box and expects to sit inside a
  padded area (`Section` default padding 20 px). The two pages put it flush (`Section padded={false}` on Upload,
  full-bleed under a 36 px header on Master Content). Padding inside the core component would double the inset on
  every page that already uses it correctly, and would be a kit core change shipped to three consumer apps.
  Upload's filter rows move from 16 to 20 px so header, filters, table and pager share one inset.
- **F3 pages on the server.** A note ("first 200") would leave the rest of the trace unreachable. The API gains
  `offset` (default 0, so every other caller is unchanged) and returns `total` (every match), `by_channel` (matches
  per channel, for the header tiles), `offset` and `limit`. The count is one grouped query over the same joins and
  WHERE as the page, run only when the page does not already hold every match (`offset > 0` or a full page).
- **F3 seven columns instead of ten.** Measured with the kit DataTable rendered in Chromium (Poppins, compact
  cells): the old columns needed 1,467 px of min-content in the 908 px pane; the new ones need 858 px (50 px spare,
  lesson: leave ≥ 20 px). Merged: Tenant + Tour ("Tenant / tour", tour on a muted second line), Status + publish
  state (the badge, then "Published ↗" / "Not published yet"), Gates + retries (the count, then "N retries"). Topic
  and Angle keep a 150 / 140 px minimum and wrap up to 220 / 200 px; Created wraps "1 Nov 2026," over the time.
- **F3 page index tied to the filters** (`pageFor()` in `lib/contentTrace.ts`): changing a filter starts at page 1
  without a reset effect (React Compiler `set-state-in-effect` rule).
- **F4 follows the rows on screen**, including the pager's current page: while the next page loads, the pager still
  shows the page whose rows are visible, the "Refreshing…" button and the dimmed rows say that a load is running.

## Changed

- `api/routers/admin_a4.py` `GET /admin/a4/content-log`: new `offset` query parameter; `total` now counts every match
  (was `len(data)`); new response keys `by_channel`, `offset`, `limit`. `docs/api/openapi.json` regenerated with the
  pinned fastapi 0.161.1 / pydantic 2.14.1.
- The Tenant 360 Social Content tab (`/admin/tenants`, `content-log?…&limit=500` → `total`) now shows the real number
  of pieces instead of a number capped at 500. No code change there.
- Content Trace column headers: "Tenant / tour", "Gates", "Created" (were Tenant, Tour, Gate count, Retry count,
  Published, Created at). Message keys follow (EN + VI).

## Tradeoffs

- Column sort on Content Trace orders the rows of the page on screen (kit DataTable server-paging behaviour), not
  the whole match set. Server-side sort on every column would need a whitelisted ORDER BY per column; the default
  newest-first order is server side. The page size select offers 200, the old fixed load.
- Tour, Published and Retries no longer sort on their own: they sort as the second key of Tenant / tour, Status and
  Gates. The Published filter still narrows by publish state.
- The per-channel counts cost one extra grouped query only on paged views; a small first page needs none.

## Should know

- Deploy order: the frontend asks for `limit` / `offset`; an API without this change ignores `offset` and answers
  `total` = rows on the page, so until the API rollout completes the pager shows a single page (no wrong data).
- Tests: `frontend/lib/pageRange.test.mjs`, `frontend/lib/contentTrace.test.mjs`, the source guards
  `frontend/lib/adminUiS263.test.mjs`; API unit tests in `tests/unit/test_aa469_viec5_a4_feedback_loop.py`
  (`TestGetContentLogPagingAA1006`) and the real-Postgres paging test `tests/integration/test_aa1006_content_log_paging.py`.
- `frontend/eslint-suppressions.json` stays `{}`.
