# S241 batch: portal + admin (integration branch)

Branch `feat/s241-portal-admin-batch`, cut from `origin/main` at `353c328d`. Merged with `--no-ff`
in this order: `chore/s241-admin-pollers`, `fix/aa-834-smoke-hydration`, `fix/aa-836-portal-390`,
`feat/aa-863-seed-place`. Per-branch notes: `S241-admin-pollers.md`, `AA-834.md`, `AA-836.md`,
`AA-863.md`.

## Decisions
- Merge order follows the task: pollers first, so the Sidebar's shared review-count hook lands before
  the AA-834 hydration marker.
- No integration fix-up commits were needed. All four merges applied without conflicts.

## Changed
- `frontend/app/_kit/Sidebar.tsx` now carries both changes: `useReviewPendingCount()` (pollers;
  key `["review-queue", "shell-pending-count"]`, 180 s) and `useHydrated()` + `data-hydrated`
  (AA-834). Git merged them automatically because the hunks do not overlap. I read the merged file to
  check that both are there.

## Tradeoffs
- `useHydrated` is imported directly by the Sidebar and is not exported from `_kit/index.ts`. That is
  how AA-834 shipped it, and I left it that way.

## Should know
- Checks run on the merged tree:
  - `tsc --noEmit`: clean.
  - ESLint on every changed frontend file: no new errors or warnings. The 16 errors in
    `admin/atom-curation/page.tsx` (`set-state-in-effect`, `no-unescaped-entities`) are on `main`
    already. Every warning line blames to the `main` boundary commit.
  - `next build` (Turbopack): passes.
  - flake8 on changed `.py`: clean.
  - Unit tests: 290 passed. These are the tests the branches added or touched, plus the existing
    job-runner, slate and seed suites.
- `next build` refuses a symlinked `frontend/node_modules`: Turbopack says "points out of the
  filesystem root", because `turbopack.root` is the frontend dir. For the local build I used a
  hard-linked copy (`cp -al`) instead. This is local only and is gitignored.
- What was not run: Playwright smoke (AA-834 specs), the full unit suite, and any check in a browser
  or against live data.
