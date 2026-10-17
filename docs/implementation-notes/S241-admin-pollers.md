# S241 — cut CIS admin polling (Vercel Fluid Active CPU)

Chore, no Linear issue. Source numbers: `AA-Ecosys/docs/audits/2026-10-16-S240-vercel/README.md`
(CIS = 87.6 % of function GB-h; top proxied paths 3–16 Oct: verify-admin 42.2k, notifications/count
17.8k, review-queue 16.0k, job-runner summary|workers|jobs ~15k each).

## Decisions
- **Job list without blobs**: no existing param omitted payload/result, so the list endpoint gained
  `fields=full|summary` (default full, unknown value → 422). Summary selects `NULL::jsonb AS payload`
  / `NULL::jsonb AS result` so the JSON shape (keys) is unchanged — older clients and the frontend
  `Job` type keep working. `progress` stays in the list (the progress column + ETA read it).
  The drawer already reads `/jobs/{id}` (full row + links), so no drawer data change was needed.
- **Jobs page "active" signal** = any of: 30-day summary counts with queued/running n>0, a
  queued/running row on the current page, or the worker panel's live `running`/`queued` (the only
  non-time-bound view). Polling every 30 s only then; otherwise `refetchInterval: false`.
  `refetchOnWindowFocus: true` on that query so an idle page picks up work enqueued elsewhere when
  the admin returns to the tab (the app default is no focus refetch; 30 s staleTime bounds it).
  Header text now says whether auto-refresh is on or paused.
- **Review fix (mount + first-load failure)**: the jobs query sets `staleTime: 0` and
  `refetchOnMount: "always"`. Without it the global 30 s staleTime let a client-side return to
  /admin/jobs reuse the cached idle snapshot, so a job enqueued on another admin page never showed
  (poll off, focus never fires in-tab). It also keeps polling while `data` is still undefined (first
  load failed): queue state unknown → retry every 30 s, same rule as JobDrawer; the header says
  "Retrying every 30 s until the queue loads." in that state instead of "Queue idle".
- **Drawer** polls only while its job is queued/running (unknown status before the first response
  still polls, so a transient error retries). The LLM-calls query key includes the active flag, so
  the running→finished flip refetches the calls exactly once more, then stops.
- **Page size**: default 25, options `[25, 50, 100, 200]` — an old `?page_size=100` link still works
  (readView accepts any listed size; writeView omits only the default).
- **Topbar count**: react-query, key `["admin-notifications-count"]`, 120 s, no background,
  focus refetch with 60 s staleTime, `retry: false` (old code never retried either). Mark-all-read
  writes 0 into the cache. Dropdown open still fetches the 10-item list as before (unchanged).
- **Review pending badge**: one hook `app/_kit/useReviewPendingCount.ts`, key
  `["review-queue", "shell-pending-count"]` — under the `review-queue` prefix on purpose so the
  Review Queue page's existing `invalidateQueries(["review-queue"])` after approve/reject refreshes
  the badge immediately (180 s otherwise). staleTime = refetchInterval = 180 s, no background.

- **Verify cache**: admin TTL 20 s → 300 s; tenant stays 20 s (not in the audit; same class now
  takes a per-cache TTL). Every entry is capped at the JWT's own `exp` (payload decoded without
  signature check — only used to shorten an entry for a token the backend just verified). Token
  without a readable `exp` → old 20 s TTL. `get` now treats `now == expiresAt` as expired.
  Safe because `/auth/verify-admin` is stateless (signature + exp + role claim; no DB, no
  denylist): role change / deactivation already only bite at re-login or exp; logout only deletes
  the cookie and never touched this cache. Worst case (documented in the file): JWT_SECRET rotation
  leaves already-cached tokens accepted up to 5 min per warm instance (was 20 s).

- **Proxy streaming**: one helper `frontend/lib/proxyResponse.ts` `streamUpstream(res)` used by the
  three proxies — `new NextResponse(res.body, {status, Content-Type only})`. Header set is identical
  to before (only Content-Type; Content-Encoding/Length never forwarded — undici already decoded the
  body; Set-Cookie was never forwarded). Request-side code, auth, rbac, 400/403/503/502 paths untouched.

## Changed
- Backend: `shared/jobs/queue.py` `list_jobs(include_blobs=...)` + `_SUMMARY_COLUMNS`;
  `api/routers/admin_job_runner.py` `fields` query param. Unit tests:
  `tests/unit/test_s241_jobs_list_summary.py`. CONTEXT.md job-runner bullet updated.
- Frontend: `app/admin/jobs/page.tsx`, `JobDrawer.tsx`, `app/_kit/Topbar.tsx`, `Sidebar.tsx`,
  `BottomNav.tsx`, new `app/_kit/useReviewPendingCount.ts` (exported from the kit barrel),
  `lib/auth-server.ts` (also shared by `middleware.ts`), new `lib/proxyResponse.ts`,
  `app/api/{admin,tenant,pipeline}/[...path]/route.ts`.

## Tradeoffs
- A NULL-ed column keeps the response shape stable at the cost of a few bytes per row (`"payload":null`).
- Idle Jobs page no longer auto-updates: a job enqueued elsewhere shows up on focus / Refresh /
  filter change, not within 10 s. Accepted: the page is an ops view, and the live case (work
  running) still refreshes every 30 s.
- 30 s instead of 10 s makes the ETA/progress column update less often; the ETA rate math is
  per-sample-time, so it stays correct, just coarser.

- Streaming: a mid-body upstream failure now reaches the client as a truncated response with the
  upstream status instead of a clean 502. Accepted (rare; the JSON parse fails client-side the same
  way an error would).

## Should know
- **Behaviour change (fix) in the proxies**: a bodyless upstream status (204/304) used to become a
  502 — `new NextResponse(new ArrayBuffer(0), {status: 204})` throws "Invalid response status code
  204" inside the try. With `res.body` (null for 204) it now passes through. No backend route
  returns 204 today (grep), so nothing visible changes.
- Local checks done (scratchpad scripts, not committed): gzip upstream → streamed body equals the
  decoded JSON and no content-encoding header; 204 → 204; 500 → 500 with body. `tokenExpiryMs`
  decodes base64url payloads (incl. non-ASCII username) and returns null for garbage.
- Not verified here: a Vercel preview, real Fluid CPU numbers after deploy, browser behaviour of the
  pollers (tab hidden/focus). The integrate stage builds and smokes once.
- The router compares `fields` as a str (`isinstance`): a direct Python call (unit tests) receives
  the FastAPI `Query` default object, which must mean "full".
