# AA-835 — CIS admin page /admin/supply-content (list + /<id>)

Follow-up of AA-835 S6 (`AA-835-s6.md`: the admin page was cut from #991). `catalog.v_supply_content.edit_url`
(CIS 278) and Booking's "Edit in CIS" link point at `/admin/supply-content/<entity id>`; this branch builds that page
and a list entry point on the existing admin kit. Frontend only: the API of S6 (`api/routers/admin_supply_content.py`)
already carries everything the page needs. No migration.

## Decisions

- **Two routes**: `/admin/supply-content` (list) and `/admin/supply-content/[id]` (detail, a real page, not a drawer,
  because it is the deep link Booking opens). The detail reads `GET /admin/supply-content/{id}` only; the list reads
  the list (server-side status / type filters and paging, lesson S218) and `GET /status` (the feed board: pending,
  cursor, last pull, last error, draft / purge jobs 7 days, LLM cost today — observability rule).
- **Access**: `PAGE_RULES /admin/supply-content` = every staff role (the backend reads are open to every staff role).
  Write controls are hidden with `can()`: Edit / Regenerate need `content.write`, Publish / Reject / Unpublish /
  Republish `review.act` (the S6 POLICY). The viewer sees the page read-only; the BFF refuses any write of it.
  No `API_WRITE_RULES` entry: content and reviewer roles may act here (S6 RBAC decision).
- **Nav**: CIS Content › Assets › "Supply content (Booking)" (next to "AA content (CRM)"), icon `Hotel`.
- **Errors**: the S6 API answers `{detail: {error, message, …}}`, which the kit `ApiError` would print as
  "[object Object]"; `lib/supplyContent.ts supplyCall` reads it (and a FastAPI 422 list) into one message. A missing /
  malformed id shows "not reached CIS from the Booking feed yet" with a link back.
- **Publish blockers are shown before the click** (`publishBlockers`, the same rules as `review.publish`: not a draft
  and not a republish, inactive entity, written for older facts, a photo not cleared and copied). The button is not
  disabled: the backend stays the authority and its 409 message is shown as a toast and in the confirm.
- **Photo rights state** per photo (`photoState`, the `_photo_problems` rule): Cleared and copied / Not copied yet /
  No usage rights / Removed in Booking / Unknown photo, with thumbnails from the CIS copy (`url_600`).
- **Edit** opens a modal prefilled from the open draft, else the published version (`based_on_version_id`); photos
  keep their caption / alt from that base; the form checks the 422 rules first (2000 chars, ≤ 5 highlights, ≤ 3 good
  to know, 160 chars a line). Saving writes a new human draft; nothing changes in Booking until it is published.
- **Regenerate** asks first (one paid call, about $0.02, inside the daily cap and the $1 budget). No polling (S240):
  the toast says to reload when the job is done.
- "Edit facts in Booking" opens `BOOKING_ADMIN_URL + edit_in_booking_path` (lib/apps.ts; external tab).

## Changed

- `frontend/lib/supplyContent.ts` + `supplyContent.test.mjs` (new, `npm run test:unit`).
- `frontend/app/admin/supply-content/page.tsx`, `SupplyContentList.tsx`, `[id]/page.tsx`, `SupplyContentDetail.tsx`
  (new).
- `frontend/lib/rbac.ts` (PAGE_RULES row), `frontend/app/_kit/adminNav.tsx` (Assets entry), `CONTEXT.md` (the
  "admin page is a follow-up" line now describes the page).

## Tradeoffs

- The history list expands a version's text in place without photo thumbnails (it passes no media) to keep the
  section light; the open draft and the published version show their photos with the rights state.
- No word-level diff: the API's unified diff (S6 tradeoff) is coloured line by line.

## Should know

- aa-ui-verify pages for the orchestrator (Vercel preview; 1440 + 390, light + dark, console clean):
  `/admin/supply-content` (feed board, filters, paging, row click) and `/admin/supply-content/<id>` of a Dev entity in
  each state that exists (in_review with a draft and its diff; published; stale), plus the confirm modals (Publish,
  Reject, Unpublish, Regenerate — do not confirm a paid Regenerate beyond the paid-runs rule) and the Edit modal.
  Check as the read-only viewer too (no write buttons). A random uuid shows the not-found card.
- Booking's hotel link: `edit_in_booking_path` uses the Booking uuid (`/hotels?hotel=<uuid>`); S6 noted the Booking
  drawer may expect the hotel code. Verify the link on the preview; if it does not open the hotel, it is an API change
  (`review.booking_path`), not a page change.
