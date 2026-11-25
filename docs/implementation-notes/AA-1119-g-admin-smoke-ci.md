# AA-1119 — admin route check (S292 lane g-admin-smoke-ci)

Follow-up of the S291 hotfix (CIS #1679): the Ecosystem status page reached Dev with ack and saved-view controls
calling `useToast()` outside a `ToastProvider`. CI never rendered it as the administrator: the per-PR visual run has no
session and the signed-in smoke / visual runs use the E2E viewer, which cannot open admin-only pages. This adds
`.github/workflows/admin-routes.yml` (job `Admin routes (next start, stub API, administrator)`).

## Decisions

- **Every page the administrator may open, not only admin-only ones.** The viewer sees the shared pages without their
  write controls, which is where provider hooks like `useToast` live. 60 page routes + 11 query variants; 48 routes are
  admin-only (the report marks them).
- **Routes from the code**: walk `frontend/app/admin/**/page.tsx` (route groups dropped, private `_x` folders skipped),
  keep what `lib/rbac.ts canSeePage("admin")` opens. Dynamic segments get an invented UUID-shaped id (the pages read it
  client-side). Query variants (tabs) are borrowed from `tests/e2e/visual/pages.ts` for routes already in the list.
- **No credential, no runtime change**: a throwaway `next build` with `NEXT_PUBLIC_API_URL` (proxy.ts, inlined at build)
  and `API_URL` (BFF, run time) pointed at `tests/e2e/admin-routes/stub-api.mjs` on 127.0.0.1. The stub answers
  `POST /auth/verify-admin` with the administrator for any token, `GET /admin/session` writable (so `WriteOnly` controls
  render) and 503 to everything else. The browser sends an opaque stand-in `cis_admin_token` (not a JWT, so it is a
  legacy-shaped session: no Cognito refresh path). The real proxy gate and BFF run unchanged.
- **Error mode, not fixtures**: every data read answers 503, so each page renders its "unavailable" state. A generic
  `{}` or `[]` answer would crash pages on shape (false reds); per-endpoint fixtures (Booking's layout-mock) are a
  larger follow-up. Error mode caught the S291 crash because the saved-views control renders regardless of data.
- **Failure rules** (`routes.ts problemsOf`): uncaught error, console error (except the browser's "Failed to load
  resource" for same-origin `/api/*`, which the stub causes on purpose), no `[data-kit-frame]` (named "error boundary"
  when the global error page text is on screen), or a final URL outside `/admin`.
- **Self-test in the same run**: three planted faults on `/admin/overview` via `addInitScript` (an uncaught
  `useToast must be used within a <ToastProvider>`, a console error, the global error page in place of the shell); each
  test passes only when the run names its fault.
- **Known problems**: `KNOWN_PROBLEMS` tolerates a precise problem per path with a reason and follow-up; an entry that
  stops matching fails the run (stale), so the fix PR deletes it. One entry on the first run: `/admin/s1-rewrite`
  (`fetchTours` rethrows a failed read with no catch, an unhandled rejection; the page still renders).
- Required-check shape copied from `visual.yml`: no `paths:`, merge_group trigger, step-level Scope skip.
- Runs on pull_request as well as merge_group (unlike Visual since S271): the point is to catch the crash on the PR.
  It only does work when the PR touches frontend or the check's files.

## Changed

- No Linear issue of its own (S291 follow-up, release safety): Refs AA-1119.
- No change to `frontend/` runtime code, Cognito, the sign-in page or the BFF.

## Tradeoffs

- A second `next build` per frontend PR (about 1 min in CI, plus npm ci and the Chromium install; the page run is about
  45 s on 4 workers). Reusing the Visual build is not possible: that one has no API URL pointed at a stub and runs only
  in the queue.
- Error mode only exercises each page's error branch and the always-rendered controls; a component that renders only
  with data is not covered (fixtures are the follow-up).

## Should know

- Proof against the S291 regression (local, scratch revert of #1679, not in the PR): the run went red on
  `/admin/overview` with `console error: Error: useToast must be used within a <ToastProvider>` and
  `error boundary on screen: "Something went wrong"`; 70 other tests passed. With the hotfix in place: 71/71 green,
  twice in a row (about 45 s each).
- Making it a required check is a branch-protection change (the tech lead's script), after its first green run.
- Local run: see the header of `tests/e2e/admin-routes/admin-routes.spec.ts`.
