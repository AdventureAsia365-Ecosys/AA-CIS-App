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

## Changed
- Backend: `shared/jobs/queue.py` `list_jobs(include_blobs=...)` + `_SUMMARY_COLUMNS`;
  `api/routers/admin_job_runner.py` `fields` query param. Unit tests:
  `tests/unit/test_s241_jobs_list_summary.py`. CONTEXT.md job-runner bullet updated.
- Frontend: `app/admin/jobs/page.tsx`, `JobDrawer.tsx`, `app/_kit/Topbar.tsx`, `Sidebar.tsx`,
  `BottomNav.tsx`, new `app/_kit/useReviewPendingCount.ts` (exported from the kit barrel).

## Tradeoffs
- A NULL-ed column keeps the response shape stable at the cost of a few bytes per row (`"payload":null`).
- Idle Jobs page no longer auto-updates: a job enqueued elsewhere shows up on focus / Refresh /
  filter change, not within 10 s. Accepted: the page is an ops view, and the live case (work
  running) still refreshes every 30 s.
- 30 s instead of 10 s makes the ETA/progress column update less often; the ETA rate math is
  per-sample-time, so it stays correct, just coarser.

## Should know
- The router compares `fields` as a str (`isinstance`): a direct Python call (unit tests) receives
  the FastAPI `Query` default object, which must mean "full".
